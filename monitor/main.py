"""Entry point: python -m monitor [--dry-run] [--test-notify] [--config PATH]"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time

import requests

from .config import Config, ConfigError, PageConfig, load_config
from .fetchers import FetchError, fetch_posts
from .models import Post
from .net import new_session
from .notifiers import Notifier, NotifyError, build_notifiers
from .state import State

log = logging.getLogger("monitor")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="monitor", description="Facebook page -> Discord/Telegram alerts")
    parser.add_argument("--config", help="path to config.json (default: ./config.json or $CONFIG_PATH)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and log only; send nothing, save nothing")
    parser.add_argument("--test-notify", action="store_true", help="send one sample alert to every notifier and exit")
    return parser.parse_args(argv)


def _deliver(post: Post, notifiers: list[Notifier], cfg: Config) -> bool:
    """True if at least one notifier accepted the post."""
    delivered = False
    for notifier in notifiers:
        if cfg.dry_run:
            log.info("[dry-run] would send via %s: %s", notifier.name, post.url)
            delivered = True
            continue
        try:
            notifier.send(post)
            delivered = True
            log.info("Sent via %s: %s", notifier.name, post.url)
        except NotifyError as exc:
            log.error("%s failed for %s: %s", notifier.name, post.url, exc)
        except Exception:  # noqa: BLE001
            log.exception("%s crashed for %s", notifier.name, post.url)
    return delivered


def _process_page(page: PageConfig, cfg: Config, state: State, session: requests.Session,
                  notifiers: list[Notifier]) -> None:
    posts = fetch_posts(page, cfg, session)  # raises FetchError
    key = page.key
    seen = state.seen_ids(key)
    new_posts = [p for p in posts if p.id not in seen]

    if not state.is_initialized(key) and not cfg.notify_on_first_run:
        log.info("[%s] first run: baselining %d existing posts (no alerts)", page.display_name, len(posts))
        state.add_seen(key, [p.id for p in posts])
        state.mark_initialized(key)
        return
    state.mark_initialized(key)

    if not new_posts:
        log.info("[%s] no new posts", page.display_name)
        return

    to_send = new_posts[: cfg.max_posts_per_run]          # page order is newest-first
    overflow = new_posts[cfg.max_posts_per_run:]
    if overflow:
        log.warning("[%s] %d extra new posts skipped (max_posts_per_run=%d)", page.display_name,
                    len(overflow), cfg.max_posts_per_run)
        state.add_seen(key, [p.id for p in overflow])

    for post in reversed(to_send):                         # alert oldest -> newest
        if _deliver(post, notifiers, cfg):
            state.add_seen(key, [post.id])                 # only remember what was actually delivered
            time.sleep(1)                                  # stay under webhook rate limits
        else:
            log.error("[%s] delivery failed; will retry on next run", page.display_name)
            break


def _test_notify(notifiers: list[Notifier]) -> int:
    if not notifiers:
        print("No notifier configured. Set DISCORD_WEBHOOK_URL and/or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.")
        return 2
    sample = Post(id="test", url="https://www.facebook.com/", page_name="Test Page",
                  text="✅ If you can read this, your alert pipeline works.")
    failures = 0
    for notifier in notifiers:
        try:
            notifier.send(sample)
            print(f"{notifier.name}: OK")
        except NotifyError as exc:
            failures += 1
            print(f"{notifier.name}: FAILED - {exc}")
    return 1 if failures else 0


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        cfg.dry_run = True
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    session = new_session()
    notifiers = build_notifiers(cfg, session)
    if args.test_notify:
        return _test_notify(notifiers)
    if not cfg.pages:
        log.error("No pages configured (config.json 'pages' or FB_PAGES).")
        return 2
    if not notifiers and not cfg.dry_run:
        log.error("No notifier configured: set DISCORD_WEBHOOK_URL and/or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.")
        return 2

    state = State(cfg.state_file, cfg.seen_limit)
    attempted = failed = 0
    try:
        for page in cfg.pages:
            if not state.due(page.key, page.check_interval_minutes):
                log.info("[%s] skipped (interval %d min not elapsed)", page.display_name, page.check_interval_minutes)
                continue
            if attempted:
                time.sleep(random.uniform(*cfg.page_delay_range))  # look less like a bot
            attempted += 1
            try:
                _process_page(page, cfg, state, session, notifiers)
            except FetchError as exc:
                failed += 1
                log.error("[%s] fetch failed: %s", page.display_name, exc)
            except Exception:  # noqa: BLE001
                failed += 1
                log.exception("[%s] unexpected error", page.display_name)
            else:
                if page.check_interval_minutes > 0:
                    state.mark_checked(page.key)
    finally:
        if not cfg.dry_run and state.save():
            log.info("State file updated")

    if attempted and failed == attempted:
        log.error("All %d page(s) failed - Facebook is probably blocking this IP.", attempted)
        return 1  # non-zero => GitHub emails you about the failed run
    return 0

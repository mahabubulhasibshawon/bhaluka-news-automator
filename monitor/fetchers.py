"""Fetch strategies, tried in the configured order until one returns posts.

  html        plain HTTP to mbasic/m.facebook.com (fast, but often login-walled
              from datacenter IPs such as GitHub Actions runners)
  playwright  headless Chromium renders the public page, dismisses the login popup
  rss         your own RSS bridge/RSSHub URL per page (most reliable if you have one)
"""
from __future__ import annotations

import logging
import random
import time

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import Config, PageConfig
from .facebook_parser import FB_ORIGIN, extract_post_ref, looks_blocked, parse_posts
from .models import Post
from .net import MOBILE_USER_AGENTS, USER_AGENTS, request_with_retry
from .utils import truncate

log = logging.getLogger(__name__)


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """Facebook served a login wall / checkpoint / rate limit."""


# ---------------------------------------------------------------- html ----
def fetch_html(page: PageConfig, cfg: Config, session: requests.Session) -> list[Post]:
    problems: list[str] = []
    blocked = False
    for i, host in enumerate(("mbasic.facebook.com", "m.facebook.com")):
        if i:
            time.sleep(random.uniform(1, 3))
        try:
            resp = request_with_retry(
                session, "GET", page.url_for(host),
                retries=cfg.max_retries, timeout=cfg.request_timeout,
                ua_pool=MOBILE_USER_AGENTS, headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        except requests.RequestException as exc:
            problems.append(f"{host}: {type(exc).__name__}")
            continue
        if resp.status_code in (401, 403, 429):
            blocked = True
            problems.append(f"{host}: HTTP {resp.status_code}")
            continue
        if resp.status_code != 200:
            problems.append(f"{host}: HTTP {resp.status_code}")
            continue
        posts = parse_posts(
            resp.text, base_url=resp.url, page_name=page.name, fallback_name=page.slug,
            slug_filter=page.slug_filter, source=f"html:{host}",
        )
        if posts:
            return posts
        if looks_blocked(resp.text, resp.url):
            blocked = True
            problems.append(f"{host}: login wall")
        else:
            problems.append(f"{host}: no posts found")
    raise (BlockedError if blocked else FetchError)("; ".join(problems))


# ----------------------------------------------------------- playwright ----
def _dismiss_popups(pg) -> None:
    selectors = [
        '[data-cookiebanner="accept_button"]',
        'button:has-text("Decline optional cookies")',
        'button:has-text("Allow all cookies")',
        'div[role="dialog"] [aria-label="Close"]',
        '[aria-label="Close"]',
    ]
    for selector in selectors:
        try:
            pg.locator(selector).first.click(timeout=1500)
            pg.wait_for_timeout(400)
        except Exception:  # noqa: BLE001 - popups are optional
            pass
    try:
        pg.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass


def _render(url: str, timeout_s: int) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    chrome_uas = [ua for ua in USER_AGENTS if "Chrome" in ua]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            context = browser.new_context(
                user_agent=random.choice(chrome_uas),
                viewport={"width": 1366, "height": 900},
                locale="en-US",
                timezone_id="UTC",
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            pg = context.new_page()
            pg.set_default_timeout(timeout_s * 1000)
            pg.goto(url, wait_until="domcontentloaded")
            pg.wait_for_timeout(random.randint(2500, 4500))
            _dismiss_popups(pg)
            for _ in range(3):
                try:
                    pg.mouse.wheel(0, 1600)
                except Exception:  # noqa: BLE001
                    break
                pg.wait_for_timeout(random.randint(900, 1600))
            return pg.content(), pg.url
        finally:
            browser.close()


def fetch_playwright(page: PageConfig, cfg: Config, session: requests.Session) -> list[Post]:
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError as exc:
        raise FetchError("playwright is not installed") from exc

    url = page.url_for("www.facebook.com")
    last_error = "unknown error"
    for attempt in range(1, max(1, min(cfg.max_retries, 3)) + 1):
        try:
            html, final_url = _render(url, cfg.request_timeout)
        except PlaywrightError as exc:
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:150] if str(exc) else ''}"
            log.warning("[%s] playwright attempt %d failed: %s", page.display_name, attempt, last_error)
            time.sleep(2 ** attempt + random.random())
            continue
        posts = parse_posts(
            html, base_url=final_url, page_name=page.name, fallback_name=page.slug,
            slug_filter=page.slug_filter, source="playwright",
        )
        if posts:
            return posts
        if looks_blocked(html, final_url):
            raise BlockedError("login wall / checkpoint in rendered page")
        last_error = "no posts found in rendered page"
        time.sleep(2 ** attempt + random.random())
    raise FetchError(last_error)


# ------------------------------------------------------------------ rss ----
def fetch_rss(page: PageConfig, cfg: Config, session: requests.Session) -> list[Post]:
    resp = request_with_retry(
        session, "GET", page.rss_url, retries=cfg.max_retries, timeout=cfg.request_timeout, ua_pool=USER_AGENTS
    )
    if resp.status_code != 200:
        raise FetchError(f"RSS HTTP {resp.status_code}")
    feed = feedparser.parse(resp.content)
    if not feed.entries:
        raise FetchError("RSS feed has no entries")
    name = page.name or (feed.feed.get("title") if feed.feed else None) or page.slug

    posts: list[Post] = []
    seen: set[str] = set()
    for entry in feed.entries[:25]:
        link = entry.get("link") or ""
        ref = extract_post_ref(link, FB_ORIGIN, None) if link else None
        post_id, url = ref if ref else (entry.get("id") or link, link)
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        soup = BeautifulSoup(entry.get("summary") or entry.get("title") or "", "html.parser")
        img = soup.find("img", src=True)
        media = (entry.get("media_content") or [{}])[0].get("url")
        posts.append(
            Post(
                id=str(post_id), url=url or FB_ORIGIN, page_name=name,
                text=truncate(soup.get_text("\n", strip=True), 1500),
                image_url=media or (img["src"] if img else None), source="rss",
            )
        )
    return posts


STRATEGIES = {"html": fetch_html, "playwright": fetch_playwright, "rss": fetch_rss}


def fetch_posts(page: PageConfig, cfg: Config, session: requests.Session) -> list[Post]:
    """Try each configured strategy in order; return the first non-empty result."""
    errors: list[str] = []
    for name in cfg.strategies:
        if name == "rss" and not page.rss_url:
            continue
        try:
            posts = STRATEGIES[name](page, cfg, session)
        except FetchError as exc:
            errors.append(f"{name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad strategy must not kill the run
            log.debug("Unexpected error in %s", name, exc_info=True)
            errors.append(f"{name}: unexpected {type(exc).__name__}")
            continue
        if posts:
            log.info("[%s] %d posts via %s", page.display_name, len(posts), name)
            return posts
        errors.append(f"{name}: no posts")
    raise FetchError(" | ".join(errors) or "no usable strategy configured")

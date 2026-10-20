"""Configuration: config.json plus environment-variable overrides.

Secrets (webhook URL, bot token, chat IDs) are read ONLY from environment
variables so they never end up in the repository.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_STRATEGIES = ["html", "playwright", "rss"]
VALID_STRATEGIES = set(DEFAULT_STRATEGIES)


class ConfigError(ValueError):
    pass


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None  # empty string (unset GitHub secret/var) == not set


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None


@dataclass
class PageConfig:
    url: str
    name: str | None = None
    rss_url: str | None = None
    check_interval_minutes: int = 0
    slug: str = field(init=False)
    slug_filter: str | None = field(init=False)
    path: str = field(init=False)

    def __post_init__(self) -> None:
        raw = (self.url or "").strip()
        if not raw:
            raise ConfigError("Empty Facebook page URL")
        if not re.match(r"^https?://", raw, re.I):
            raw = "https://www.facebook.com/" + raw.lstrip("/")
        parsed = urlparse(raw)
        host = parsed.netloc.lower()
        if not (host in {"facebook.com", "fb.com"} or host.endswith((".facebook.com", ".fb.com"))):
            raise ConfigError(f"Not a Facebook URL: {self.url}")
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise ConfigError(f"No page name in URL: {self.url}")

        if parts[0] == "profile.php":
            page_id = parse_qs(parsed.query).get("id", [""])[0]
            if not page_id:
                raise ConfigError(f"profile.php URL needs ?id=...: {self.url}")
            self.path, self.slug, self.slug_filter = f"/profile.php?id={page_id}", f"id-{page_id}", None
        elif parts[0] in {"pages", "people"} and len(parts) >= 3:
            self.path, self.slug, self.slug_filter = "/" + "/".join(parts), parts[-1], None
        else:
            self.path, self.slug, self.slug_filter = "/" + parts[0], parts[0], parts[0]
        self.url = f"https://www.facebook.com{self.path}"

    @property
    def key(self) -> str:
        return self.slug.lower()

    @property
    def display_name(self) -> str:
        return self.name or self.slug

    def url_for(self, host: str) -> str:
        return f"https://{host}{self.path}"


@dataclass
class Config:
    pages: list[PageConfig]
    strategies: list[str]
    discord_webhook_url: str | None
    discord_username: str
    discord_avatar_url: str | None
    discord_color: int
    telegram_bot_token: str | None
    telegram_chat_ids: list[str]
    telegram_disable_preview: bool
    notify_on_first_run: bool
    max_posts_per_run: int
    seen_limit: int
    state_file: Path
    request_timeout: int
    max_retries: int
    page_delay_range: tuple[float, float]
    dry_run: bool
    log_level: str


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path or _env("CONFIG_PATH") or "config.json")
    raw: dict = {}
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in {cfg_path}: {exc}") from exc
    discord_raw = raw.get("discord", {}) or {}
    telegram_raw = raw.get("telegram", {}) or {}

    # --- pages ---------------------------------------------------------
    env_pages = _env("FB_PAGES")
    entries = [u for u in re.split(r"[,\s]+", env_pages) if u] if env_pages else raw.get("pages", [])
    default_interval = _env_int("CHECK_INTERVAL_MINUTES", int(raw.get("check_interval_minutes", 0)))
    pages: list[PageConfig] = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict) or "url" not in entry:
            raise ConfigError(f"Each page needs a 'url': {entry!r}")
        page = PageConfig(
            url=entry["url"],
            name=entry.get("name") or None,
            rss_url=entry.get("rss_url") or None,
            check_interval_minutes=int(entry.get("check_interval_minutes", default_interval)),
        )
        if all(p.key != page.key for p in pages):
            pages.append(page)

    # --- strategies ----------------------------------------------------
    env_strategies = _env("STRATEGIES")
    strategies = (
        [s.strip().lower() for s in env_strategies.split(",") if s.strip()]
        if env_strategies
        else [str(s).lower() for s in raw.get("strategies", DEFAULT_STRATEGIES)]
    )
    unknown = set(strategies) - VALID_STRATEGIES
    if unknown:
        raise ConfigError(f"Unknown strategies {sorted(unknown)}; valid: {sorted(VALID_STRATEGIES)}")

    # --- notifiers (secrets from env only) ------------------------------
    discord_on = bool(discord_raw.get("enabled", True))
    telegram_on = bool(telegram_raw.get("enabled", True))
    chat_ids = [c.strip() for c in (_env("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]

    delay = raw.get("page_delay_seconds", [3, 8])
    return Config(
        pages=pages,
        strategies=strategies,
        discord_webhook_url=_env("DISCORD_WEBHOOK_URL") if discord_on else None,
        discord_username=discord_raw.get("username") or "FB News Alerts",
        discord_avatar_url=discord_raw.get("avatar_url") or None,
        discord_color=int(discord_raw.get("color", 1603570)),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN") if telegram_on else None,
        telegram_chat_ids=chat_ids if telegram_on else [],
        telegram_disable_preview=bool(telegram_raw.get("disable_web_page_preview", False)),
        notify_on_first_run=_env_bool("NOTIFY_ON_FIRST_RUN", bool(raw.get("notify_on_first_run", False))),
        max_posts_per_run=_env_int("MAX_POSTS_PER_RUN", int(raw.get("max_posts_per_run", 5))),
        seen_limit=int(raw.get("seen_limit", 300)),
        state_file=Path(_env("STATE_FILE") or raw.get("state_file") or "seen_posts.json"),
        request_timeout=int(raw.get("request_timeout", 25)),
        max_retries=int(raw.get("max_retries", 3)),
        page_delay_range=(float(delay[0]), float(delay[1])),
        dry_run=_env_bool("DRY_RUN", False),
        log_level=(_env("LOG_LEVEL") or raw.get("log_level") or "INFO").upper(),
    )

"""Discord (webhook embeds) and Telegram (Bot API) notifiers."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Protocol

import requests

from .config import Config
from .models import Post
from .net import request_with_retry
from .utils import truncate

log = logging.getLogger(__name__)


class NotifyError(Exception):
    pass


class Notifier(Protocol):
    name: str

    def send(self, post: Post) -> None: ...


class DiscordNotifier:
    name = "discord"

    def __init__(self, cfg: Config, session: requests.Session) -> None:
        self.url = cfg.discord_webhook_url or ""
        self.username = cfg.discord_username
        self.avatar_url = cfg.discord_avatar_url
        self.color = cfg.discord_color
        self.retries, self.timeout, self.session = cfg.max_retries, cfg.request_timeout, session
        if not self.url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
            log.warning("DISCORD_WEBHOOK_URL does not look like a Discord webhook URL")

    def build_payload(self, post: Post) -> dict:
        embed: dict = {
            "title": truncate(f"New post from {post.page_name}", 256),
            "url": post.url,
            "description": truncate(post.text, 1500) or "*No text preview available — open the link to view the post.*",
            "color": self.color,
            "author": {"name": truncate(post.page_name, 256)},
            "footer": {"text": "Facebook"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if post.image_url:
            embed["image"] = {"url": post.image_url}
        payload: dict = {
            "username": self.username,
            "embeds": [embed],
            "allowed_mentions": {"parse": []},  # never let post text ping @everyone
        }
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url
        return payload

    def send(self, post: Post) -> None:
        try:
            resp = request_with_retry(
                self.session, "POST", self.url, json=self.build_payload(post),
                retries=self.retries, timeout=self.timeout,
                headers={"User-Agent": "DiscordBot (https://github.com, 1.0)"},
            )
        except requests.RequestException as exc:
            raise NotifyError(f"network error ({type(exc).__name__})") from None
        if resp.status_code not in (200, 204):
            raise NotifyError(f"HTTP {resp.status_code}: {resp.text[:200]}")


class TelegramNotifier:
    name = "telegram"

    def __init__(self, cfg: Config, session: requests.Session) -> None:
        self.token = cfg.telegram_bot_token or ""
        self.chat_ids = cfg.telegram_chat_ids
        self.disable_preview = cfg.telegram_disable_preview
        self.retries, self.timeout, self.session = cfg.max_retries, cfg.request_timeout, session

    def _call(self, method: str, payload: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            resp = request_with_retry(
                self.session, "POST", url, json=payload, retries=self.retries, timeout=self.timeout,
                headers={"User-Agent": "fb-page-monitor/1.0"},
            )
        except requests.RequestException as exc:
            raise NotifyError(f"network error ({type(exc).__name__})") from None  # URL contains the token
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not data.get("ok"):
            raise NotifyError(f"{method} HTTP {resp.status_code}: {data.get('description', resp.text[:200])}")
        return data

    @staticmethod
    def _format(post: Post, text_limit: int) -> str:
        parts = [f"📰 <b>{html.escape(post.page_name)}</b>"]
        body = truncate(post.text, text_limit)
        if body:
            parts.append(html.escape(body))
        parts.append(f'<a href="{html.escape(post.url, quote=True)}">Open on Facebook</a>')
        return "\n\n".join(parts)

    def _send_to(self, chat_id: str, post: Post) -> None:
        if post.image_url:
            try:  # captions max out at 1024 chars
                self._call("sendPhoto", {"chat_id": chat_id, "photo": post.image_url,
                                         "caption": self._format(post, 700), "parse_mode": "HTML"})
                return
            except NotifyError as exc:  # Telegram can't fetch expiring FB CDN URLs sometimes
                log.debug("sendPhoto failed (%s); falling back to text", exc)
        self._call("sendMessage", {"chat_id": chat_id, "text": self._format(post, 3000),
                                   "parse_mode": "HTML", "disable_web_page_preview": self.disable_preview})

    def send(self, post: Post) -> None:
        errors, delivered = [], 0
        for chat_id in self.chat_ids:
            try:
                self._send_to(chat_id, post)
                delivered += 1
            except NotifyError as exc:
                errors.append(f"chat {chat_id}: {exc}")
        if not delivered:
            raise NotifyError("; ".join(errors))
        if errors:
            log.warning("Telegram partial failure: %s", "; ".join(errors))


def build_notifiers(cfg: Config, session: requests.Session) -> list[Notifier]:
    notifiers: list[Notifier] = []
    if cfg.discord_webhook_url:
        notifiers.append(DiscordNotifier(cfg, session))
    if cfg.telegram_bot_token and cfg.telegram_chat_ids:
        notifiers.append(TelegramNotifier(cfg, session))
    elif cfg.telegram_bot_token or cfg.telegram_chat_ids:
        log.warning("Telegram needs BOTH TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; skipping Telegram")
    return notifiers

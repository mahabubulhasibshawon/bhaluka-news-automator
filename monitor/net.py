"""HTTP helpers: browser-like headers, user-agent rotation, retry with backoff."""
from __future__ import annotations

import logging
import random
import time

import requests

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]
MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]
MAX_SLEEP = 60.0


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _retry_after(resp: requests.Response) -> float | None:
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    params = body.get("parameters")
    value = body.get("retry_after") or (params.get("retry_after") if isinstance(params, dict) else None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int, base: float) -> float:
    return base ** (attempt + 1) + random.uniform(0, 1.5)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: float = 20,
    ua_pool: list[str] | None = None,
    **kwargs,
) -> requests.Response:
    """Request with exponential backoff on network errors, HTTP 429 and 5xx.

    Honors Retry-After / Discord `retry_after` / Telegram `parameters.retry_after`.
    Never logs the URL (it may contain a bot token or webhook secret).
    Returns the final response (even if it is a 429/5xx) once retries run out.
    """
    headers = dict(kwargs.pop("headers", None) or {})
    for attempt in range(retries + 1):
        if ua_pool:
            headers["User-Agent"] = random.choice(ua_pool)
        try:
            resp = session.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            if attempt >= retries:
                raise
            delay = _backoff(attempt, backoff)
            log.warning("Network error (%s); retry %d/%d in %.1fs", type(exc).__name__, attempt + 1, retries, delay)
            time.sleep(delay)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt >= retries:
                return resp
            delay = min(_retry_after(resp) or _backoff(attempt, backoff), MAX_SLEEP)
            log.warning("HTTP %d; retry %d/%d in %.1fs", resp.status_code, attempt + 1, retries, delay)
            time.sleep(delay)
            continue
        return resp
    raise RuntimeError("unreachable")

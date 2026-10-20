"""Tiny JSON state store (seen post IDs per page), committed back to the repo."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: str | Path, seen_limit: int = 300) -> None:
        self.path = Path(path)
        self.seen_limit = seen_limit
        self._data = self._load()
        self._original = self._dump()

    # -- persistence ------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "pages": {}}
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
                raise ValueError("unexpected structure")
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            backup = self.path.with_suffix(".corrupt.json")
            log.error("State file %s is corrupt (%s); saved copy to %s and starting fresh", self.path, exc, backup)
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return {"version": 1, "pages": {}}

    def _dump(self) -> str:
        return json.dumps(self._data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def save(self) -> bool:
        """Write atomically; returns True only if the file content changed."""
        payload = self._dump()
        if payload == self._original:
            return False
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, "utf-8")
        os.replace(tmp, self.path)
        self._original = payload
        return True

    # -- accessors --------------------------------------------------------
    def _entry(self, key: str) -> dict:
        return self._data["pages"].setdefault(key, {"initialized": False, "seen": [], "last_checked": None})

    def _peek(self, key: str) -> dict:
        return self._data["pages"].get(key, {})

    def is_initialized(self, key: str) -> bool:
        return bool(self._peek(key).get("initialized"))

    def mark_initialized(self, key: str) -> None:
        self._entry(key)["initialized"] = True

    def seen_ids(self, key: str) -> set[str]:
        return set(self._peek(key).get("seen", []))

    def add_seen(self, key: str, ids: list[str]) -> None:
        entry = self._entry(key)
        existing = set(entry["seen"])
        for post_id in ids:
            if post_id not in existing:
                entry["seen"].append(post_id)
                existing.add(post_id)
        entry["seen"] = entry["seen"][-self.seen_limit:]

    def due(self, key: str, interval_minutes: int) -> bool:
        if interval_minutes <= 0:
            return True
        last = self._peek(key).get("last_checked")
        if not last:
            return True
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
        except ValueError:
            return True
        return elapsed >= interval_minutes - 2  # 2 min slack for cron jitter

    def mark_checked(self, key: str) -> None:
        self._entry(key)["last_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

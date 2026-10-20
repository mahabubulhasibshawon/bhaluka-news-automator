from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Post:
    id: str
    url: str
    page_name: str
    text: str = ""
    image_url: str | None = None
    source: str = ""

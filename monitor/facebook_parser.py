"""Layout-agnostic Facebook HTML parsing.

Facebook's CSS class names are obfuscated and change constantly, so instead of
relying on them we look for *links that point at posts* (stable URL shapes such
as /<page>/posts/<id>, /story.php?story_fbid=..., /videos/<id>) and then walk up
to the enclosing role="article" element to get the text and image.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import Post
from .utils import truncate

FB_ORIGIN = "https://www.facebook.com"

_PATH_PATTERNS = [
    re.compile(r"^/(?P<slug>[^/?#]+)/posts/(?P<id>pfbid\w+|\d+)"),
    re.compile(r"^/(?P<slug>[^/?#]+)/videos/(?:[^/?#]+/)?(?P<id>\d+)"),
    re.compile(r"^/(?P<slug>[^/?#]+)/photos/[^/?#]+/(?P<id>\d+)"),
    re.compile(r"^/reel/(?P<id>\d+)"),
]

_NOISE = re.compile(
    r"^(like|comment|share|send|follow|see more|reply|most relevant|write a comment…?|all reactions:?"
    r"|\d+([.,]\d+)?\s?[kKmM]?( comments?| shares?)?"
    r"|\d+\s?(s|m|h|d|w|min|mins|hr|hrs|hours?|days?|weeks?)"
    r"|yesterday at.*|[·•])$",
    re.I,
)
_BLOCK_MARKERS = (
    'id="login_form"',
    "you must log in to continue",
    "log in to continue",
    "log into facebook",
    "/login/?next",
    "checkpoint",
    "security check required",
)


def extract_post_ref(href: str, base_url: str, slug_filter: str | None) -> tuple[str, str] | None:
    """Return (post_id, canonical_url) if `href` links to a post, else None."""
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    parsed = urlparse(urljoin(base_url, href))
    host = parsed.netloc.lower()
    if not (host.endswith("facebook.com") or host.endswith("fb.com")):
        return None
    path, query = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

    if path in ("/story.php", "/permalink.php"):
        story_id = (query.get("story_fbid") or [None])[0]
        owner_id = (query.get("id") or [None])[0]
        if not story_id:
            return None
        url = f"{FB_ORIGIN}{path}?story_fbid={story_id}" + (f"&id={owner_id}" if owner_id else "")
        return story_id, url

    if path == "/watch":
        video_id = (query.get("v") or [None])[0]
        return (video_id, f"{FB_ORIGIN}/watch/?v={video_id}") if video_id else None

    for pattern in _PATH_PATTERNS:
        match = pattern.match(parsed.path)
        if not match:
            continue
        slug = match.groupdict().get("slug")
        if slug and slug_filter and slug.lower() != slug_filter.lower():
            return None  # a post from some other page (suggested/related content)
        return match["id"], f"{FB_ORIGIN}{match.group(0)}"
    return None


def extract_page_name(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"property": "og:title"})
    title = meta.get("content") if meta and meta.get("content") else (soup.title.get_text() if soup.title else "")
    title = re.sub(r"^\(\d+\+?\)\s*", "", title or "")
    title = re.sub(r"\s*[|\-–—]\s*(Facebook|Log in.*)$", "", title, flags=re.I).strip()
    if not title or title.lower() in {"facebook", "log in to facebook", "log into facebook", "error", "security check required"}:
        return None
    return title


def looks_blocked(html: str, final_url: str = "") -> bool:
    """Only meaningful when no posts were parsed (logged-out pages always mention 'login')."""
    url = final_url.lower()
    if "/login" in url or "checkpoint" in url:
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def _container(anchor: Tag) -> Tag | None:
    node = anchor.find_parent(attrs={"role": "article"}) or anchor.find_parent("article")
    if node is None:
        return None
    node = copy.copy(node)
    for nested in node.find_all(attrs={"role": "article"}):  # drop comments
        nested.decompose()
    return node


def _clean_text(node: Tag | None, page_name: str | None) -> str:
    if node is None:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in node.get_text("\n", strip=True).splitlines():
        line = raw.strip()
        if len(line) <= 2 or _NOISE.match(line) or line in seen:
            continue
        if page_name and line.lower() == page_name.lower():
            continue
        seen.add(line)
        lines.append(line)
    return truncate("\n".join(lines), 1500)


def _to_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _find_image(node: Tag | None) -> str | None:
    if node is None:
        return None
    for img in node.find_all("img"):
        src = img.get("src") or ""
        if not src.startswith("http") or not ("scontent" in src or "fbcdn" in src):
            continue
        if "emoji" in src or "/rsrc.php/" in src or re.search(r"[sp]\d{2,3}x\d{2,3}", src):
            continue  # emoji, UI sprites, small avatar crops
        width, height = _to_int(img.get("width")), _to_int(img.get("height"))
        if (width and width < 200) or (height and height < 200):
            continue
        return src
    return None


def parse_posts(
    html: str,
    *,
    base_url: str,
    page_name: str | None,
    fallback_name: str,
    slug_filter: str | None,
    source: str,
    limit: int = 25,
) -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    name = page_name or extract_page_name(soup) or fallback_name
    posts: dict[str, Post] = {}
    for anchor in soup.find_all("a", href=True):
        ref = extract_post_ref(anchor["href"], base_url, slug_filter)
        if not ref or ref[0] in posts:
            continue
        post_id, url = ref
        container = _container(anchor)
        posts[post_id] = Post(
            id=post_id,
            url=url,
            page_name=name,
            text=_clean_text(container, name),
            image_url=_find_image(container),
            source=source,
        )
        if len(posts) >= limit:
            break
    return list(posts.values())

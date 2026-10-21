import json

from monitor.config import load_config
from monitor.facebook_parser import parse_posts
from monitor.state import State

SAMPLE = """
<html><head><meta property="og:title" content="Daily News | Facebook"></head><body>
<div role="article"><h3>Daily News</h3>
  <a href="/dailynews/posts/pfbid02abcDEF123?__cft__[0]=xyz&__tn__=%2CO">2h</a>
  <div>Breaking: Parliament passes the new budget after a long debate.</div>
  <img src="https://scontent.xx.fbcdn.net/v/t39/photo_big.jpg" width="600" height="400">
  <img src="https://scontent.xx.fbcdn.net/v/t1/s40x40/avatar.jpg" width="40" height="40">
  <span>Like</span><span>Comment</span><span>Share</span>
  <div role="article"><a href="/dailynews/posts/pfbid02abcDEF123?comment_id=9">c</a>Nice one!</div>
</div>
<div role="article"><a href="/story.php?story_fbid=123456789&id=555&__tn__=x">1d</a>
  <div>Second post text here.</div></div>
<div role="article"><a href="/otherpage/posts/777">3d</a><div>Other page post</div></div>
</body></html>"""


def test_parser_extracts_posts_text_and_image():
    posts = parse_posts(SAMPLE, base_url="https://m.facebook.com/dailynews", page_name=None,
                        fallback_name="dailynews", slug_filter="dailynews", source="test")
    assert [p.id for p in posts] == ["pfbid02abcDEF123", "123456789"]
    first = posts[0]
    assert first.page_name == "Daily News"
    assert first.url == "https://www.facebook.com/dailynews/posts/pfbid02abcDEF123"
    assert first.text == "Breaking: Parliament passes the new budget after a long debate."
    assert first.image_url.endswith("photo_big.jpg")
    assert posts[1].url == "https://www.facebook.com/story.php?story_fbid=123456789&id=555"


def test_state_roundtrip_and_trim(tmp_path):
    path = tmp_path / "seen.json"
    s = State(path, seen_limit=3)
    assert s.save() is False                      # nothing changed -> no write
    s.add_seen("page", ["1", "2", "3", "4"])
    s.mark_initialized("page")
    assert s.save() is True
    again = State(path, seen_limit=3)
    assert again.seen_ids("page") == {"2", "3", "4"} and again.is_initialized("page")


def test_state_recovers_from_corruption(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert State(path).seen_ids("x") == set()


def test_config_env_overrides(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"pages": ["https://www.facebook.com/a"]}))
    for var in ("TELEGRAM_BOT_TOKEN", "STRATEGIES", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FB_PAGES", "https://www.facebook.com/b, https://facebook.com/c/posts/1")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1,2")
    monkeypatch.setenv("STRATEGIES", "")           # empty (unset GitHub var) must be ignored
    cfg = load_config(cfg_file)
    assert [p.slug for p in cfg.pages] == ["b", "c"]
    assert cfg.discord_webhook_url and cfg.telegram_chat_ids == ["1", "2"]
    assert cfg.strategies == ["html", "playwright", "rss"]

# Facebook Page Monitor → Discord / Telegram (GitHub Actions)

Polls public Facebook pages on a cron schedule, detects new posts, and sends alerts to Discord (rich embeds)
and/or Telegram. State lives in `seen_posts.json`, committed back to the repo by the workflow. No server, no cost.

## Read this first (honest limits)
- **Not truly instant.** GitHub cron is 5 min minimum and often delayed; 15–30 min is realistic.
- **Facebook fights scrapers.** Datacenter IPs (GitHub runners) are frequently served a login wall.
  The monitor tries `html` → `playwright` → `rss` and exits non-zero if *every* page failed (GitHub emails you).
  If you get blocked persistently, the dependable fix is an RSS bridge (RSSHub / RSS-Bridge) set as `rss_url`.
- Scraping may violate Facebook's Terms of Service. Use at low frequency, for pages you're allowed to monitor.
- **Public repo = free unlimited minutes**, but `config.json` and `seen_posts.json` are visible (secrets are not).
  Private repos get 2,000 free min/month; Playwright runs every 20 min can exceed that.
- GitHub disables scheduled workflows after 60 days without repo activity; re-enable under the Actions tab if needed.

## Layout
```
.github/workflows/monitor.yml   # cron + manual runs, commits seen_posts.json
monitor/
  main.py            # orchestration, dedupe, first-run baseline, exit codes
  config.py          # config.json + env overrides
  fetchers.py        # html / playwright / rss strategies + fallback chain
  facebook_parser.py # layout-agnostic post extraction
  notifiers.py       # Discord embeds + Telegram Bot API
  net.py             # UA rotation, retry/backoff, 429 handling
  state.py           # atomic JSON state
tests/test_monitor.py
config.json  seen_posts.json  requirements.txt
```

## Setup
1. **Create the repo**: new GitHub repo (public recommended), upload all these files, keep the folder structure.
2. **Discord**: Server Settings → Integrations → Webhooks → New Webhook → pick channel → *Copy Webhook URL*.
3. **Telegram**: chat with `@BotFather` → `/newbot` → copy the token. Then get your chat ID:
   - Private chat: message your bot once, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, read `message.chat.id`.
   - Group: add the bot, send a message, same `getUpdates` URL (ID is negative).
   - Channel: add the bot as admin; use `@channelusername` or the `-100…` ID.
   Multiple chats: comma-separate in `TELEGRAM_CHAT_ID`.
4. **Secrets**: repo → Settings → Secrets and variables → Actions → *Secrets*:
   `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (set either or both channels).
5. **Pages**: edit `config.json` `pages`, or set a repo *Variable* `FB_PAGES` (comma-separated URLs; overrides the file).
6. **Enable Actions**: Actions tab → enable workflows. Then *Run workflow* with **test_notify = true** to confirm alerts arrive.
7. **Dry run**: *Run workflow* with **dry_run = true** and read the logs to see whether Facebook lets you in.
8. **Go live**: the first real run baselines existing posts silently (no spam); later new posts alert.

## Configuration
| Setting | Where | Default |
|---|---|---|
| Pages | `config.json` `pages` / var `FB_PAGES` | – |
| Run interval | cron line in `monitor.yml` | every 20 min |
| Per-page throttle | `check_interval_minutes` (per page or var `CHECK_INTERVAL_MINUTES`) | 0 (every run) |
| Strategies / order | `strategies` / var `STRATEGIES` | `html,playwright,rss` |
| Skip Chromium install (faster) | var `USE_PLAYWRIGHT=false` | install |
| Alert on first run | `notify_on_first_run` / var `NOTIFY_ON_FIRST_RUN` | false |
| Max alerts per page per run | `max_posts_per_run` / var `MAX_POSTS_PER_RUN` | 5 |
| Webhook / token / chat | **secrets only** | – |

`check_interval_minutes` writes `last_checked` into the state file, so those pages create a commit on each check.

## Local use
```bash
pip install -r requirements.txt && playwright install chromium
export DISCORD_WEBHOOK_URL=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python -m monitor --test-notify     # send a sample alert
python -m monitor --dry-run         # fetch + log only
python -m monitor                   # real run
python -m pytest                    # tests
```

## Troubleshooting
- `login wall` / `HTTP 403/429`: Facebook blocked the runner. Add an `rss_url`, lower frequency, or run from a home machine.
- Alerts have no text/image: Facebook layout changed or the page is partially rendered; links still work.
- Telegram `chat not found`: message the bot first (private) or make it a channel admin.

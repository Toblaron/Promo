# promo

Tracks the **2b-lob** YouTube channel, spots what's working, and writes platform-ready posts
that send listeners to **[fritog.lat](https://fritog.lat)**. Every link is UTM-tagged, so your site
analytics (Plausible, Umami, GA, anything) shows which platform each visitor came from.

- **Zero dependencies.** One stdlib Python file plus SQLite. The image is `python:3.13-slim` plus a single 43 KB script.
- **Read-only API key.** Public data via the YouTube Data API v3. No OAuth, and it can't touch your channel.
- **Hardened container.** Non-root, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, healthcheck,
  128 MB memory cap.

## Quick start

```bash
cp .env.example .env     # set YT_API_KEY (instructions inside)
make up                  # build and start the watch daemon
make top                 # what's performing
make promo               # posts for your newest upload
```

`make up` fails fast and creates `.env` for you if it's missing.

## What the daemon does

`watch` runs every `WATCH_INTERVAL` (default `6h`):

| Trigger | Action |
|---|---|
| every cycle | snapshot channel and per-track stats into `data/promoter.db` |
| new upload | release posts for all 8 platforms → `data/out/promo/`, plus a notification |
| track crosses 100 / 250 / 500 / 1k / 2.5k / 5k / 10k … views | milestone posts plus a notification |
| every 7 days | report → `data/out/reports/`, plus spotlight posts for the **push candidate** |

The first run is a silent baseline, so adding a channel never floods you with "new upload" alerts.
The push candidate is the track with the best engagement rate (likes + comments ÷ views, ≥ 50 views)
that isn't your newest release. People who find it tend to like it, so it's the one worth pushing.

Notifications go to ntfy (self-hosted or ntfy.sh, with optional bearer token) and/or a Discord
webhook. Each one includes the ready-to-paste X post. If neither is configured, it just logs.

## Commands

```bash
make sync                                   # snapshot now
make top BY=gain N=5                        # views | gain | velocity | engagement | likes | comments | recent
make growth DAYS=30                         # channel + track deltas over a window
make promo ARGS="--top 3 --by gain"         # spotlight posts for this week's risers
make promo ARGS="--video https://youtu.be/ID --kind milestone --platforms x,bluesky"
make report DAYS=7
make cli ARGS="top --json"                  # anything, JSON for piping into n8n / scripts
make logs | make ps | make down
make test                                   # test suite in a throwaway container, no API key needed
```

Without Docker: `python3 promoter.py <command>` (Python 3.11+). It reads `.env` from the
working directory and stores data in `./data/`.

`watch --once` runs a single cycle and exits non-zero on failure, for cron or systemd timers.
`health` exits 0 if a cycle succeeded within 2 × interval; the Docker healthcheck uses it.

## Platforms

| Platform | Limit | Hashtags | Notes |
|---|---|---|---|
| X | 280 (URLs count 23) | 3 | |
| Bluesky | 300 | 3 | URLs counted at full length (conservative) |
| Mastodon | 500 (URLs count 23) | 5 | CamelCase tags for screen readers |
| Threads | 500 | 1 | Threads allows one topic tag per post |
| Instagram | 2,200 | 15 | captions can't link, so it points to "link in bio" |
| Reddit | 300 (title) | – | `Artist - Title [Genre]` link post + a first comment with the site link |
| Discord | 2,000 | – | site link wrapped in `<>` so only the YouTube player embeds |
| YouTube | description footer | 3 | site link + `?sub_confirmation=1` subscribe link; first 3 tags render above the title |

Posts that run long drop hashtags first, then shorten the title. They never exceed the limit.
Hashtags come from `PROMO_HASHTAGS`, then each video's own YouTube tags. Hook lines vary per
track and platform but are deterministic, so re-running gives identical output.

UTM scheme: `utm_source=<platform>&utm_medium=social&utm_campaign=<track-slug>`. YouTube
description links use `utm_medium=video_description&utm_content=<video_id>`.

## Quota

The default allowance is 10,000 units/day. A sync costs `1 + 2·⌈videos/50⌉` units per channel,
so a 200-track channel costs about 9 units. `search.list` (100 units) is never used. The uploads
playlist enumerates every upload exactly, unlike search.

## Layout

```
promoter.py          the whole tool
tests/               pytest suite + a fake YouTube API (python tests/fake_youtube.py 8765 for offline runs)
Dockerfile           compose.yaml   Makefile   .env.example
data/                (gitignored) promoter.db, out/promo/*.md, out/reports/*.md
```

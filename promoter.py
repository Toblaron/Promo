#!/usr/bin/env python3
"""
promoter: YouTube channel stats -> platform-ready promo posts -> traffic to your site.

Zero third-party dependencies. Reads public data through the YouTube Data API v3
(API key only, no OAuth), keeps a snapshot history in SQLite, and writes
UTM-tagged posts so your site analytics can attribute every visit to a platform.

    promoter sync | top | growth | promo | report | watch | health
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path

__version__ = "1.0.0"

API_BASE = "https://www.googleapis.com/youtube/v3"
ISO = "%Y-%m-%dT%H:%M:%SZ"
UA = f"promoter/{__version__}"
HEARTBEAT = ".heartbeat"
MIN_INTERVAL = 300
MILESTONES = (
    100,
    250,
    500,
    1_000,
    2_500,
    5_000,
    10_000,
    25_000,
    50_000,
    100_000,
    250_000,
    500_000,
    1_000_000,
    5_000_000,
    10_000_000,
)
SORT_KEYS = ("views", "gain", "velocity", "engagement", "likes", "comments", "recent")

# platform: (char limit, weight per URL or None = counted literally, max hashtags)
PLATFORMS: dict[str, tuple[int, int | None, int]] = {
    "x": (280, 23, 3),
    "bluesky": (300, None, 3),
    "mastodon": (500, 23, 5),
    "threads": (500, None, 1),
    "instagram": (2200, None, 15),
    "reddit": (300, None, 0),  # limit applies to the title
    "discord": (2000, None, 0),
    "youtube": (5000, None, 3),  # description footer; first 3 hashtags render above the title
}
LABELS = {
    "x": "X",
    "bluesky": "Bluesky",
    "mastodon": "Mastodon",
    "threads": "Threads",
    "instagram": "Instagram",
    "reddit": "Reddit",
    "discord": "Discord",
    "youtube": "YouTube description footer",
}
HOOKS = {
    "release": ("New track out now", "Fresh release", "Just dropped", "Out now"),
    "spotlight": ("From the catalogue", "Worth another spin", "In case you missed it", "Back in rotation"),
    "milestone": ("Thank you for {m} views", "Just passed {m} views", "{m} plays and counting"),
}


# ── helpers ──────────────────────────────────────────────────────────────────


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def fmt_ts(d: datetime) -> str:
    return d.astimezone(UTC).strftime(ISO)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def log(msg: str) -> None:
    print(f"{fmt_ts(utcnow())} {msg}", file=sys.stderr, flush=True)


def _int(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def iso_duration(s: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def parse_interval(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", s or "")
    if not m:
        raise ValueError(f"invalid interval {s!r} (use e.g. 30m, 6h, 1d)")
    return int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def human(n) -> str:
    n = n or 0
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div * 0.9995:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def num(n) -> str:
    return "–" if n is None else f"{n:,}"


def signed(n) -> str:
    return "–" if n is None else f"{n:+,}"


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "đ": "d", "ł": "l", "Ł": "L"})


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.translate(_TRANSLIT)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def hashtag(s: str) -> str:
    """'melodic techno' -> '#MelodicTechno' (CamelCase reads better with screen readers)."""
    words = re.findall(r"[^\W_]+", s.strip().lstrip("#"))
    if not words or len(words) > 3:
        return ""
    if len(words) == 1 and words[0] != words[0].lower():
        tag = words[0]  # already cased by the user (e.g. 2bLob): keep it
    else:
        tag = "".join(w[:1].upper() + w[1:] for w in words)
    return f"#{tag}" if len(tag) <= 30 else ""


def merge_tags(base, extra) -> list[str]:
    out, seen = [], set()
    for tag in [*base, *map(hashtag, extra)]:
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def utm(site: str, source: str, medium: str, campaign: str, content: str = "") -> str:
    u = urllib.parse.urlsplit(site)
    q = urllib.parse.parse_qsl(u.query) + [("utm_source", source), ("utm_medium", medium), ("utm_campaign", campaign)]
    if content:
        q.append(("utm_content", content))
    return urllib.parse.urlunsplit(u._replace(path=u.path or "/", query=urllib.parse.urlencode(q)))


def video_id(s: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", s)
    return m[1] if m else s.strip()


_URL = re.compile(r"https?://\S+")


def measure(text: str, url_weight: int | None = None) -> int:
    """Conservative length: URLs at the platform's fixed weight, astral chars (emoji) as 2."""
    if url_weight:
        text = _URL.sub("u" * url_weight, text)
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def pick(options, *seed: str):
    """Deterministic variety: same track+platform always gets the same hook."""
    return options[int(hashlib.sha256("|".join(seed).encode()).hexdigest(), 16) % len(options)]


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.removeprefix("export ").strip(), v.strip().strip("'\""))


# ── config ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    api_key: str = ""
    channels: tuple[str, ...] = ("@2b-lob",)
    artist: str = "2b-lob"
    site_url: str = "https://fritog.lat"
    hashtags: tuple[str, ...] = ()
    genre: str = ""
    db_path: Path = Path("data/promoter.db")
    out_dir: Path = Path("data/out")
    api_base: str = API_BASE
    ntfy_url: str = ""
    ntfy_token: str = ""
    discord_webhook: str = ""
    watch_interval: int = 6 * 3600

    @classmethod
    def from_env(cls) -> Config:
        def e(key: str, default: str = "") -> str:
            return os.environ.get(key, default).strip()

        site = e("SITE_URL", cls.site_url)
        if not re.match(r"https?://", site):
            site = f"https://{site}"
        return cls(
            api_key=e("YT_API_KEY"),
            channels=tuple(c.strip() for c in e("YT_CHANNELS", "@2b-lob").split(",") if c.strip()),
            artist=e("ARTIST_NAME", cls.artist),
            site_url=site.rstrip("/"),
            hashtags=tuple(t for t in map(hashtag, e("PROMO_HASHTAGS", "NewMusic,ElectronicMusic").split(",")) if t),
            genre=e("GENRE"),
            db_path=Path(e("DB_PATH", str(cls.db_path))),
            out_dir=Path(e("OUT_DIR", str(cls.out_dir))),
            api_base=e("YT_API_BASE", API_BASE).rstrip("/"),
            ntfy_url=e("NTFY_URL"),
            ntfy_token=e("NTFY_TOKEN"),
            discord_webhook=e("DISCORD_WEBHOOK_URL"),
            watch_interval=parse_interval(e("WATCH_INTERVAL", "6h")),
        )


# ── YouTube Data API v3 ──────────────────────────────────────────────────────


class YouTubeError(RuntimeError):
    def __init__(self, msg: str, status: int = 0, reason: str = ""):
        super().__init__(msg)
        self.status, self.reason = status, reason


_CHANNEL_URL = re.compile(r"youtube\.com/(?:channel/(?P<id>UC[\w-]{22})|(?P<handle>@[^/?#\s]+))")


def channel_params(ref: str) -> dict[str, str]:
    ref = ref.strip()
    if m := _CHANNEL_URL.search(ref):
        ref = m["id"] or m["handle"]
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return {"id": ref}
    return {"forHandle": ref if ref.startswith("@") else f"@{ref}"}


def _api_error(body: str) -> tuple[str, str]:
    try:
        err = json.loads(body)["error"]
        return (err.get("errors") or [{}])[0].get("reason", ""), err.get("message", "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return "", body[:200]


class YouTube:
    """Read-only client. Cost: 1 quota unit per call (≈ 1 + 2·⌈videos/50⌉ per channel sync)."""

    backoff = 1.0

    def __init__(self, cfg: Config):
        if not cfg.api_key:
            raise YouTubeError("YT_API_KEY is not set (see .env.example)")
        self.key, self.base, self.units = cfg.api_key, cfg.api_base, 0

    def _get(self, endpoint: str, **params) -> dict:
        url = f"{self.base}/{endpoint}?{urllib.parse.urlencode({**params, 'key': self.key})}"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.units += 1
                    return json.load(r)
            except urllib.error.HTTPError as ex:
                reason, message = _api_error(ex.read().decode("utf-8", "replace"))
                if ex.code >= 500 and attempt < 3:
                    time.sleep(self.backoff * 2**attempt)
                    continue
                if ex.code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    raise YouTubeError(
                        f"API quota exhausted ({reason}); resets at midnight Pacific", ex.code, reason
                    ) from None
                detail = " ".join(x for x in (reason, message) if x).replace(self.key, "***")
                raise YouTubeError(f"{endpoint}: HTTP {ex.code} {detail}".rstrip(), ex.code, reason) from None
            except (urllib.error.URLError, TimeoutError) as ex:
                if attempt < 3:
                    time.sleep(self.backoff * 2**attempt)
                    continue
                raise YouTubeError(f"{endpoint}: {getattr(ex, 'reason', ex)}") from None
        raise YouTubeError(f"{endpoint}: retries exhausted")

    def channel(self, ref: str) -> dict:
        items = self._get("channels", part="snippet,statistics,contentDetails", **channel_params(ref)).get("items")
        if not items:
            raise YouTubeError(f"channel not found: {ref}", 404)
        return items[0]

    def playlist_video_ids(self, playlist_id: str) -> list[str]:
        ids: list[str] = []
        token = None
        while True:
            params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
            if token:
                params["pageToken"] = token
            try:
                data = self._get("playlistItems", **params)
            except YouTubeError as ex:
                if ex.status == 404:  # empty uploads playlist -> playlistNotFound
                    return ids
                raise
            ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
            token = data.get("nextPageToken")
            if not token:
                return ids

    def videos(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            out += self._get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i : i + 50])).get(
                "items", []
            )
        return out


# ── storage ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY, title TEXT, handle TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS channel_stats (
  channel_id TEXT NOT NULL, ts TEXT NOT NULL, subscribers INTEGER, views INTEGER, videos INTEGER,
  PRIMARY KEY (channel_id, ts));
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, title TEXT, published_at TEXT,
  duration_s INTEGER, tags TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS video_stats (
  video_id TEXT NOT NULL, ts TEXT NOT NULL, views INTEGER NOT NULL, likes INTEGER, comments INTEGER,
  PRIMARY KEY (video_id, ts));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""
LATEST_SQL = """SELECT s.* FROM video_stats s
JOIN (SELECT video_id, MAX(ts) AS ts FROM video_stats GROUP BY video_id) l
  ON l.video_id = s.video_id AND l.ts = s.ts"""
UPSERT_CHANNEL = """INSERT INTO channels (id, title, handle, updated_at) VALUES (?,?,?,?)
ON CONFLICT(id) DO UPDATE SET title=excluded.title, handle=excluded.handle, updated_at=excluded.updated_at"""
UPSERT_VIDEO = """INSERT INTO videos (id, channel_id, title, published_at, duration_s, tags, first_seen)
VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,
published_at=excluded.published_at, duration_s=excluded.duration_s, tags=excluded.tags"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def get_meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


# ── sync ─────────────────────────────────────────────────────────────────────


@dataclass
class SyncResult:
    baseline: bool
    channels: int = 0
    tracks: int = 0
    new: list[str] = field(default_factory=list)
    milestones: list[tuple[str, int]] = field(default_factory=list)
    units: int = 0


def sync(cfg: Config, db: sqlite3.Connection, yt: YouTube, now: datetime | None = None) -> SyncResult:
    """Snapshot every channel + upload. Network first, then one atomic write."""
    if not cfg.channels:
        raise YouTubeError("YT_CHANNELS is empty")
    ts = fmt_ts(now or utcnow())
    fetched = []
    for ref in cfg.channels:
        ch = yt.channel(ref)
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            log(f"warn: {ref} exposes no uploads playlist")
        fetched.append((ch, yt.videos(yt.playlist_video_ids(uploads)) if uploads else []))

    known = dict(db.execute("SELECT id, channel_id FROM videos").fetchall())
    known_channels = set(known.values())
    prev = {r["video_id"]: r["views"] for r in db.execute(LATEST_SQL)}
    res = SyncResult(baseline=not known, units=yt.units)
    with db:
        for ch, videos in fetched:
            cid, sn, st = ch["id"], ch.get("snippet", {}), ch.get("statistics", {})
            fresh_channel = cid not in known_channels  # first sight of a channel never counts as "new uploads"
            db.execute(UPSERT_CHANNEL, (cid, sn.get("title", ""), sn.get("customUrl", ""), ts))
            db.execute(
                "INSERT OR REPLACE INTO channel_stats VALUES (?,?,?,?,?)",
                (
                    cid,
                    ts,
                    None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
                    _int(st.get("viewCount")),
                    _int(st.get("videoCount")),
                ),
            )
            res.channels += 1
            for v in videos:
                vid, vsn, vst = v["id"], v.get("snippet", {}), v.get("statistics", {})
                views = _int(vst.get("viewCount")) or 0
                published = fmt_ts(parse_ts(vsn["publishedAt"])) if vsn.get("publishedAt") else ts
                db.execute(
                    UPSERT_VIDEO,
                    (
                        vid,
                        cid,
                        vsn.get("title", ""),
                        published,
                        iso_duration(v.get("contentDetails", {}).get("duration", "")),
                        json.dumps(vsn.get("tags", []), ensure_ascii=False),
                        ts,
                    ),
                )
                db.execute(
                    "INSERT OR REPLACE INTO video_stats VALUES (?,?,?,?,?)",
                    (vid, ts, views, _int(vst.get("likeCount")), _int(vst.get("commentCount"))),
                )
                res.tracks += 1
                if vid not in known and not fresh_channel:
                    res.new.append(vid)
                old = prev.get(vid)
                if old is not None and (crossed := [m for m in MILESTONES if old < m <= views]):
                    res.milestones.append((vid, crossed[-1]))
    return res


# ── analytics ────────────────────────────────────────────────────────────────


def load_tracks(db: sqlite3.Connection, now: datetime) -> list[dict]:
    """Latest stats per track; tracks missing from their channel's last sync (deleted/private) drop out."""
    rows = db.execute(f"""
        SELECT v.*, s.views, s.likes, s.comments, s.ts AS stats_ts, c.handle
        FROM videos v
        JOIN ({LATEST_SQL}) s ON s.video_id = v.id
        LEFT JOIN channels c ON c.id = v.channel_id
        WHERE s.ts = (SELECT MAX(ts) FROM channel_stats WHERE channel_id = v.channel_id)
    """).fetchall()
    tracks = []
    for r in rows:
        t = dict(r)
        t["tags"] = json.loads(t["tags"] or "[]")
        t["age_days"] = max((now - parse_ts(t["published_at"])).total_seconds() / 86400, 1.0)
        t["velocity"] = t["views"] / t["age_days"]
        t["engagement"] = ((t["likes"] or 0) + (t["comments"] or 0)) / t["views"] if t["views"] else 0.0
        tracks.append(t)
    return tracks


def with_growth(db: sqlite3.Connection, tracks: list[dict], days: int, now: datetime) -> list[dict]:
    cutoff = fmt_ts(now - timedelta(days=days))
    for t in tracks:
        row = db.execute(
            "SELECT views FROM video_stats WHERE video_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (t["id"], cutoff)
        ).fetchone()
        if row:
            base, partial = row[0], False
        elif t["published_at"] >= cutoff:
            base, partial = 0, False  # released inside the window: every view counts
        else:
            base = db.execute(
                "SELECT views FROM video_stats WHERE video_id=? ORDER BY ts LIMIT 1", (t["id"],)
            ).fetchone()[0]
            partial = True  # tracking started mid-window: lower bound
        t["gain"], t["gain_partial"] = t["views"] - base, partial
    return tracks


def channel_summary(db: sqlite3.Connection, days: int, now: datetime) -> list[dict]:
    cutoff = fmt_ts(now - timedelta(days=days))
    q = "SELECT * FROM channel_stats WHERE channel_id=?"
    out = []
    for c in db.execute("SELECT * FROM channels ORDER BY title").fetchall():
        cur = db.execute(q + " ORDER BY ts DESC LIMIT 1", (c["id"],)).fetchone()
        if not cur:
            continue
        base = db.execute(q + " AND ts<=? ORDER BY ts DESC LIMIT 1", (c["id"], cutoff)).fetchone()
        partial = base is None
        base = base or db.execute(q + " ORDER BY ts LIMIT 1", (c["id"],)).fetchone()

        d_subs, d_views = (
            None if cur[k] is None or base[k] is None else cur[k] - base[k] for k in ("subscribers", "views")
        )
        out.append(
            {
                "id": c["id"],
                "title": c["title"],
                "handle": c["handle"],
                "subscribers": cur["subscribers"],
                "views": cur["views"],
                "videos": cur["videos"],
                "d_subscribers": d_subs,
                "d_views": d_views,
                "since": base["ts"],
                "partial": partial,
            }
        )
    return out


def push_candidate(tracks: list[dict]) -> dict | None:
    """Best engagement among tracks with real exposure, excluding the newest release (already pushed)."""
    if len(tracks) < 2:
        return None
    newest = max(tracks, key=lambda t: t["published_at"])["id"]
    rest = [t for t in tracks if t["id"] != newest]
    pool = [t for t in rest if t["views"] >= 50] or rest
    return max(pool, key=lambda t: (t["engagement"], t["velocity"]))


def sort_key(by: str):
    return (lambda t: t["published_at"]) if by == "recent" else (lambda t: t.get(by) or 0)


def ranked(rows: list[dict]) -> list[dict]:
    return [dict(r, rank=i) for i, r in enumerate(rows, 1)]


def public(t: dict) -> dict:
    return {
        "id": t["id"],
        "title": t["title"],
        "url": f"https://youtu.be/{t['id']}",
        "published_at": t["published_at"],
        "views": t["views"],
        "likes": t["likes"],
        "comments": t["comments"],
        "engagement": round(t["engagement"], 4),
        "views_per_day": round(t["velocity"], 2),
        "gain": t.get("gain"),
        "gain_partial": t.get("gain_partial"),
    }


# ── tables ───────────────────────────────────────────────────────────────────


def track_cols(days: int):
    return [
        ("#", lambda t: str(t["rank"]), ">"),
        ("Title", lambda t: clip(t["title"], 40), "<"),
        ("Views", lambda t: num(t["views"]), ">"),
        (f"+{days}d", lambda t: num(t["gain"]) + ("*" if t["gain_partial"] else ""), ">"),
        ("Likes", lambda t: num(t["likes"]), ">"),
        ("Cmts", lambda t: num(t["comments"]), ">"),
        ("Eng%", lambda t: f"{t['engagement'] * 100:.1f}", ">"),
        ("/day", lambda t: f"{t['velocity']:.1f}", ">"),
        ("Released", lambda t: t["published_at"][:10], "<"),
        ("ID", lambda t: t["id"], "<"),
    ]


CHANNEL_COLS = [
    ("Channel", lambda c: c["handle"] or c["title"], "<"),
    ("Subs", lambda c: num(c["subscribers"]), ">"),
    ("Δ", lambda c: signed(c["d_subscribers"]), ">"),
    ("Views", lambda c: num(c["views"]), ">"),
    ("Δ", lambda c: signed(c["d_views"]), ">"),
    ("Videos", lambda c: num(c["videos"]), ">"),
    ("Since", lambda c: c["since"][:10] + ("*" if c["partial"] else ""), "<"),
]


def render_table(rows: list[dict], cols, md: bool = False) -> str:
    head = [h for h, _, _ in cols]
    body = [[str(f(r)) for _, f, _ in cols] for r in rows]
    if md:
        sep = ["---:" if a == ">" else ":---" for _, _, a in cols]
        return "\n".join("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in [head, sep, *body])
    widths = [max(map(len, col)) for col in zip(head, *body, strict=True)]

    def line(r):
        return "  ".join(
            c.rjust(w) if a == ">" else c.ljust(w) for c, w, (_, _, a) in zip(r, widths, cols, strict=True)
        ).rstrip()

    return "\n".join([line(head), line(["-" * w for w in widths]), *map(line, body)])


# ── promo posts ──────────────────────────────────────────────────────────────


@dataclass
class Post:
    platform: str
    text: str
    length: int
    limit: int
    title: str = ""  # reddit
    url: str = ""  # reddit


def _md_escape(s: str) -> str:
    return re.sub(r"([\\*_`~|>])", r"\\\1", s)


def _render(p: str, c: dict, title: str, tags: list[str]) -> str:
    a = c["artist"]
    if p == "reddit":
        return f"{a} - {title}" + (f" [{c['genre']}]" if c["genre"] else "")
    if p == "discord":  # <url> suppresses the site embed so the YouTube player stays the only embed
        blocks = [f"**{c['hook']}** — {a} · *{_md_escape(title)}*", c["yt"], f"More at <{c['site']}>"]
    elif p == "instagram":  # captions can't link
        blocks = [f"{c['hook']} 🎧 {a} — {title}", f"Full track on YouTube, more at {c['domain']} (link in bio)"]
    elif p == "youtube":
        blocks = [f"🌐 {c['site']}\n🔔 Subscribe: {c['channel_url']}?sub_confirmation=1"]
    else:  # x, bluesky, mastodon, threads
        blocks = [f"{c['hook']}: {a} — {title}", f"▶ {c['yt']}\n🌐 {c['site']}"]
    if tags:
        blocks.append(" ".join(tags))
    return "\n\n".join(blocks)


def _fit(p: str, c: dict, title: str, tags: list[str], limit: int, url_weight: int | None) -> tuple[str, int]:
    """Drop hashtags from the end first, then shorten the title, until the post fits."""
    tags = list(tags)
    while True:
        text = _render(p, c, title, tags)
        n = measure(text, url_weight)
        if n <= limit:
            return text, n
        if tags:
            tags.pop()
            continue
        keep = len(title) - (n - limit) - 1
        if keep < 12:
            return text, n
        title = title[:keep].rstrip() + "…"


def build_posts(cfg: Config, t: dict, kind: str, platforms, milestone: int | None = None) -> list[Post]:
    yt = f"https://youtu.be/{t['id']}"
    campaign = slugify(t["title"])[:40].strip("-") or t["id"]
    tags = merge_tags(cfg.hashtags, t.get("tags") or [])
    handle = t.get("handle") or ""
    channel_url = (
        f"https://www.youtube.com/{handle}"
        if handle.startswith("@")
        else f"https://www.youtube.com/channel/{t.get('channel_id', '')}"
    )
    posts = []
    for p in platforms:
        limit, url_weight, max_tags = PLATFORMS[p]
        medium, content = ("video_description", t["id"]) if p == "youtube" else ("social", "")
        c = {
            "artist": cfg.artist,
            "genre": cfg.genre,
            "yt": yt,
            "channel_url": channel_url,
            "domain": urllib.parse.urlsplit(cfg.site_url).netloc,
            "site": utm(cfg.site_url, p, medium, campaign, content),
            "hook": pick(HOOKS[kind], kind, t["id"], p).format(m=human(milestone or t["views"])),
        }
        text, n = _fit(p, c, t["title"], tags[:max_tags], limit, url_weight)
        if p == "reddit":
            posts.append(Post(p, f"More releases & contact: {c['site']}", n, limit, title=text, url=yt))
        else:
            posts.append(Post(p, text, n, limit))
    return posts


def render_promo_md(cfg: Config, t: dict, kind: str, posts: list[Post], now: datetime) -> str:
    out = [
        f"# {cfg.artist} — {t['title']}",
        "",
        f"`{t['id']}` · {kind} · released {t['published_at'][:10]} · {num(t['views'])} views · generated {fmt_ts(now)}",
        "",
    ]
    for p in posts:
        out += [f"## {LABELS[p.platform]} ({p.length}/{p.limit})", ""]
        if p.platform == "reddit":
            out += [
                "Title:",
                "```text",
                p.title,
                "```",
                f"Link: {p.url}",
                "",
                "First comment:",
                "```text",
                p.text,
                "```",
                "",
            ]
        else:
            out += ["```text", p.text, "```", ""]
    return "\n".join(out)


def make_promo(
    cfg: Config, t: dict, kind: str, platforms, now: datetime, milestone: int | None = None, save: bool = True
) -> tuple[Path | None, list[Post]]:
    posts = build_posts(cfg, t, kind, platforms, milestone)
    if not save:
        return None, posts
    d = cfg.out_dir / "promo"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{now:%Y%m%d}-{kind}-{slugify(t['title'])[:40].strip('-') or 'track'}-{t['id']}.md"
    path.write_text(render_promo_md(cfg, t, kind, posts, now))
    return path, posts


def _text_for(posts: list[Post], platform: str) -> str:
    return next((p.text for p in posts if p.platform == platform), posts[0].text if posts else "")


# ── report ───────────────────────────────────────────────────────────────────


def build_report(cfg: Config, db: sqlite3.Connection, days: int, now: datetime):
    tracks = with_growth(db, load_tracks(db, now), days, now)
    chans = channel_summary(db, days, now)
    out = [
        f"# {cfg.artist} — {days}-day report",
        "",
        f"Generated {fmt_ts(now)} · window from {fmt_ts(now - timedelta(days=days))}",
        "",
        "## Channels",
        "",
        render_table(chans, CHANNEL_COLS, md=True),
        "",
    ]

    def top5(key: str) -> list[dict]:
        return sorted(tracks, key=lambda t: t[key], reverse=True)[:5]

    sections = (
        (f"Top gainers ({days}d)", [t for t in top5("gain") if t["gain"] > 0]),
        ("All-time top", top5("views")),
        ("Fastest (views/day since release)", top5("velocity")),
    )
    for name, rows in sections:
        table = render_table(ranked(rows), track_cols(days), md=True) if rows else "_No movement in this window yet._"
        out += [f"## {name}", "", table, ""]
    cand = push_candidate(tracks)
    if cand:
        out += [
            "## Push this week",
            "",
            f"**{cand['title']}** has {cand['engagement'] * 100:.1f}% engagement on {num(cand['views'])} views "
            f"and isn't your newest release: listeners who find it like it. "
            f"Posts: `promo --video {cand['id']} --kind spotlight`",
            "",
        ]
    if any(t["gain_partial"] for t in tracks) or any(c["partial"] for c in chans):
        out.append("\\* tracking started inside the window, so the gain is a lower bound.")
    return "\n".join(out) + "\n", tracks, cand


# ── notifications ────────────────────────────────────────────────────────────


def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def notify(cfg: Config, title: str, body: str) -> None:
    log(f"notify: {title}")
    if cfg.ntfy_url:
        sep = "&" if "?" in cfg.ntfy_url else "?"
        url = f"{cfg.ntfy_url}{sep}{urllib.parse.urlencode({'title': title, 'tags': 'musical_note'})}"
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
        try:
            _post(url, body.encode(), headers)
        except Exception as ex:  # notifications must never kill the daemon
            log(f"warn: ntfy failed: {ex}")
    if cfg.discord_webhook:
        payload = json.dumps({"content": f"**{title}**\n{body}"[:2000]}).encode()
        try:
            _post(cfg.discord_webhook, payload, {"Content-Type": "application/json"})
        except Exception as ex:
            log(f"warn: discord failed: {ex}")


# ── watch cycle ──────────────────────────────────────────────────────────────


def run_cycle(cfg: Config, db: sqlite3.Connection, now: datetime | None = None) -> SyncResult:
    now = now or utcnow()
    res = sync(cfg, db, YouTube(cfg), now)
    log(
        f"sync: {res.tracks} tracks · {len(res.new)} new · {len(res.milestones)} milestones · {res.units} units"
        + (" · baseline" if res.baseline else "")
    )
    tracks = {t["id"]: t for t in load_tracks(db, now)}
    platforms = list(PLATFORMS)

    new = sorted((tracks[v] for v in res.new if v in tracks), key=lambda t: t["published_at"], reverse=True)
    for t in new[:3]:
        path, posts = make_promo(cfg, t, "release", platforms, now)
        notify(cfg, f"New upload: {t['title']}", f"{_text_for(posts, 'x')}\n\n→ {path.name}")
    if len(new) > 3:
        log(f"{len(new) - 3} more new uploads skipped (run: promo --video ID)")

    for vid, m in res.milestones:
        if t := tracks.get(vid):
            path, posts = make_promo(cfg, t, "milestone", platforms, now, milestone=m)
            notify(cfg, f"{t['title']} passed {human(m)} views", f"{_text_for(posts, 'x')}\n\n→ {path.name}")

    (cfg.db_path.parent / HEARTBEAT).touch()
    weekly(cfg, db, now)
    return res


def weekly(cfg: Config, db: sqlite3.Connection, now: datetime, days: int = 7) -> Path | None:
    last = get_meta(db, "last_report")
    if last is None:
        set_meta(db, "last_report", fmt_ts(now))  # first run is the baseline
        return None
    if now - parse_ts(last) < timedelta(days=days):
        return None
    text, tracks, cand = build_report(cfg, db, days, now)
    rdir = cfg.out_dir / "reports"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{now:%Y%m%d}-weekly.md"
    path.write_text(text)
    lines = [
        f"{c['handle'] or c['title']}: {num(c['subscribers'])} subs ({signed(c['d_subscribers'])}), "
        f"{num(c['views'])} views ({signed(c['d_views'])})"
        for c in channel_summary(db, days, now)
    ]
    if tracks and (top := max(tracks, key=lambda t: t["gain"]))["gain"] > 0:
        lines.append(f"Top gainer: {top['title']} (+{top['gain']:,})")
    if cand:
        ppath, _ = make_promo(cfg, cand, "spotlight", list(PLATFORMS), now)
        lines.append(f"Push this week: {cand['title']} → {ppath.name}")
    lines.append(f"Report → {path.name}")
    notify(cfg, f"{cfg.artist} weekly report", "\n".join(lines))
    set_meta(db, "last_report", fmt_ts(now))
    return path


# ── commands ─────────────────────────────────────────────────────────────────


def _ready(cfg: Config) -> tuple[sqlite3.Connection, datetime]:
    db = open_db(cfg.db_path)
    if not db.execute("SELECT 1 FROM video_stats LIMIT 1").fetchone():
        log("no data yet, running initial sync")
        sync(cfg, db, YouTube(cfg))
    return db, utcnow()


def cmd_sync(cfg: Config, args) -> int:
    db = open_db(cfg.db_path)
    res = sync(cfg, db, YouTube(cfg))
    print(
        f"synced {res.tracks} tracks across {res.channels} channel(s) · {len(res.new)} new · "
        f"{len(res.milestones)} milestones · {res.units} API units" + (" · baseline" if res.baseline else "")
    )
    for vid, m in res.milestones:
        print(f"  milestone: {vid} passed {human(m)} views")
    for vid in res.new:
        print(f"  new: https://youtu.be/{vid}")
    return 0


def cmd_top(cfg: Config, args) -> int:
    db, now = _ready(cfg)
    tracks = sorted(with_growth(db, load_tracks(db, now), args.days, now), key=sort_key(args.by), reverse=True)
    sel = tracks[: args.n]
    if args.json:
        print(json.dumps([public(t) for t in sel], indent=2, ensure_ascii=False))
    else:
        print(render_table(ranked(sel), track_cols(args.days)))
    return 0


def cmd_growth(cfg: Config, args) -> int:
    db, now = _ready(cfg)
    chans = channel_summary(db, args.days, now)
    tracks = sorted(with_growth(db, load_tracks(db, now), args.days, now), key=lambda t: t["gain"], reverse=True)
    tracks = tracks[: args.n]
    if args.json:
        print(
            json.dumps(
                {"days": args.days, "channels": chans, "tracks": [public(t) for t in tracks]},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(render_table(chans, CHANNEL_COLS) + "\n\n" + render_table(ranked(tracks), track_cols(args.days)))
    return 0


def cmd_promo(cfg: Config, args) -> int:
    platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    if bad := [p for p in platforms if p not in PLATFORMS]:
        raise SystemExit(f"unknown platform(s): {', '.join(bad)}; choose from {', '.join(PLATFORMS)}")
    db, now = _ready(cfg)
    tracks = with_growth(db, load_tracks(db, now), args.days, now)
    if not tracks:
        raise SystemExit("no tracks found on the configured channel(s)")
    by_id = {t["id"]: t for t in tracks}
    if args.video:
        ids = [video_id(v) for v in args.video]
        if missing := [i for i in ids if i not in by_id]:
            raise SystemExit(f"not in the database (run sync first?): {', '.join(missing)}")
        sel, kind = [by_id[i] for i in ids], args.kind or "release"
    elif args.top:
        sel, kind = sorted(tracks, key=sort_key(args.by), reverse=True)[: args.top], args.kind or "spotlight"
    else:
        sel, kind = [max(tracks, key=lambda t: t["published_at"])], args.kind or "release"

    results = []
    for t in sel:
        m = max((x for x in MILESTONES if x <= t["views"]), default=None) if kind == "milestone" else None
        path, posts = make_promo(cfg, t, kind, platforms, now, m, save=not args.no_save)
        results.append((t, path, posts))
        if path:
            log(f"saved {path}")

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "video": public(t),
                        "kind": kind,
                        "file": str(path) if path else None,
                        "posts": [vars(p) for p in posts],
                    }
                    for t, path, posts in results
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print("\n".join(render_promo_md(cfg, t, kind, posts, now) for t, _, posts in results))
    return 0


def cmd_report(cfg: Config, args) -> int:
    db, now = _ready(cfg)
    text, _, _ = build_report(cfg, db, args.days, now)
    print(text)
    if not args.no_save:
        d = cfg.out_dir / "reports"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{now:%Y%m%d}-{args.days}d.md"
        path.write_text(text)
        log(f"saved {path}")
    return 0


def cmd_watch(cfg: Config, args) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 ignores SIGTERM without a handler
    interval = max(cfg.watch_interval, MIN_INTERVAL)
    db = open_db(cfg.db_path)
    log(f"watch: {', '.join(cfg.channels)} every {interval}s → {cfg.site_url}")
    while True:
        ok = True
        try:
            run_cycle(cfg, db)
        except YouTubeError as ex:
            ok = False
            log(f"error: {ex}")
        except Exception:  # keep the daemon alive; healthcheck catches persistent failure
            ok = False
            log("error: unexpected\n" + traceback.format_exc())
        if args.once:
            return 0 if ok else 2
        time.sleep(interval)


def cmd_health(cfg: Config, args) -> int:
    hb = cfg.db_path.parent / HEARTBEAT
    try:
        age = time.time() - hb.stat().st_mtime
    except FileNotFoundError:
        print("no heartbeat yet")
        return 1
    limit = 2 * max(cfg.watch_interval, MIN_INTERVAL) + 600
    print(f"last successful cycle {int(age)}s ago (limit {limit}s)")
    return 0 if age <= limit else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="promoter", description="YouTube stats → promo posts → traffic to your site.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    s = sub.add_parser("sync", help="snapshot channel + track stats")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("top", help="rank tracks")
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--by", choices=SORT_KEYS, default="views")
    s.add_argument("--days", type=int, default=7, help="window for the gain column (default 7)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_top)

    s = sub.add_parser("growth", help="channel + track growth over a window")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_growth)

    s = sub.add_parser("promo", help="generate platform-ready posts")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--video", action="append", metavar="ID|URL", help="repeatable")
    g.add_argument("--latest", action="store_true", help="newest upload (default)")
    g.add_argument("--top", type=int, metavar="N", help="top N tracks ranked by --by")
    s.add_argument("--by", choices=SORT_KEYS, default="velocity")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--kind", choices=tuple(HOOKS))
    s.add_argument("--platforms", default=",".join(PLATFORMS), help="comma-separated (default: all)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-save", action="store_true")
    s.set_defaults(func=cmd_promo)

    s = sub.add_parser("report", help="markdown report: channels, gainers, top, push candidate")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--no-save", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("watch", help="daemon: sync, auto-promo new uploads + milestones, weekly report")
    s.add_argument("--once", action="store_true", help="single cycle (cron / systemd timer)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("health", help="exit 0 if the watch daemon completed a cycle recently")
    s.set_defaults(func=cmd_health)
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        cfg = Config.from_env()
    except ValueError as ex:
        log(f"config error: {ex}")
        return 2
    try:
        return args.func(cfg, args) or 0
    except YouTubeError as ex:
        log(f"error: {ex}")
        return 2
    except PermissionError as ex:
        log(f"error: {ex}; the data dir must be writable by uid {os.getuid()} (run: make data)")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

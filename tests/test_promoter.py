import dataclasses
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import promoter as P  # noqa: E402
from fake_youtube import CID, T0, FakeYouTube, video  # noqa: E402

LATER = T0 + timedelta(days=8)


@pytest.fixture
def fake():
    f = FakeYouTube().seed().start()
    yield f
    f.stop()


@pytest.fixture
def cfg(fake, tmp_path, monkeypatch):
    env = {
        "YT_API_KEY": "SECRET-KEY-123",
        "YT_CHANNELS": "@2b-lob",
        "YT_API_BASE": fake.url,
        "DB_PATH": str(tmp_path / "data" / "promoter.db"),
        "OUT_DIR": str(tmp_path / "data" / "out"),
        "SITE_URL": "fritog.lat",
        "ARTIST_NAME": "2b-lob",
        "PROMO_HASHTAGS": "2bLob, #NewMusic",
        "GENRE": "Melodic Techno",
        "NTFY_URL": "",
        "NTFY_TOKEN": "",
        "DISCORD_WEBHOOK_URL": "",
        "WATCH_INTERVAL": "6h",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(P.YouTube, "backoff", 0)
    return P.Config.from_env()


def week_two(fake):
    """One track takes off, a new one drops, subs grow."""
    fake.set_views("trackCCCC03", 1200)
    fake.add(video("trackDDDD04", "Nattøy på Fløyen", T0 + timedelta(days=6), 150, 12, 1, ["psytrance"]))
    fake.subs = 55


def test_config_normalises(cfg):
    assert cfg.site_url == "https://fritog.lat"
    assert cfg.hashtags == ("#2bLob", "#NewMusic")
    assert cfg.watch_interval == 6 * 3600


def test_sync_baseline_then_growth(cfg, fake):
    db = P.open_db(cfg.db_path)
    r1 = P.sync(cfg, db, P.YouTube(cfg), now=T0)
    assert (r1.baseline, r1.tracks, r1.new, r1.milestones) == (True, 3, [], [])
    assert sum(ep == "playlistItems" for ep, _ in fake.calls) == 2  # paginated
    assert r1.units == 4  # channels + 2×playlistItems + videos

    week_two(fake)
    r2 = P.sync(cfg, db, P.YouTube(cfg), now=LATER)
    assert r2.new == ["trackDDDD04"]
    assert r2.milestones == [("trackCCCC03", 1000)]  # 90 → 1200 crosses 100..1000, report the top one

    tracks = {t["id"]: t for t in P.with_growth(db, P.load_tracks(db, LATER), 7, LATER)}
    assert tracks["trackCCCC03"]["gain"] == 1110 and not tracks["trackCCCC03"]["gain_partial"]
    assert tracks["trackDDDD04"]["gain"] == 150  # released inside the window
    assert tracks["trackAAAA01"]["gain"] == 0
    ch = P.channel_summary(db, 7, LATER)[0]
    assert (ch["subscribers"], ch["d_subscribers"], ch["partial"]) == (55, 15, False)


def test_deleted_track_drops_out(cfg, fake):
    db = P.open_db(cfg.db_path)
    P.sync(cfg, db, P.YouTube(cfg), now=T0)
    del fake.videos["trackAAAA01"]
    P.sync(cfg, db, P.YouTube(cfg), now=LATER)
    assert {t["id"] for t in P.load_tracks(db, LATER)} == {"trackBBBB02", "trackCCCC03"}


def test_empty_channel(cfg, fake):
    fake.videos.clear()
    db = P.open_db(cfg.db_path)
    res = P.sync(cfg, db, P.YouTube(cfg), now=T0)
    assert res.tracks == 0 and res.channels == 1


def test_failed_sync_writes_nothing(cfg, monkeypatch):
    db = P.open_db(cfg.db_path)

    def boom(self, ids):
        raise P.YouTubeError("boom")

    monkeypatch.setattr(P.YouTube, "videos", boom)  # channel call succeeds, video fetch dies
    with pytest.raises(P.YouTubeError):
        P.sync(cfg, db, P.YouTube(cfg), now=T0)
    assert db.execute("SELECT COUNT(*) FROM channel_stats").fetchone()[0] == 0


def test_posts_fit_limits_with_long_title(cfg):
    t = {
        "id": "trackAAAA01",
        "title": "Ekstremt lang tittel på en låt " * 12,
        "views": 4200,
        "tags": ["melodic techno", "dark techno", "psytrance", "bergen", "norway"],
        "published_at": "2026-01-01T00:00:00Z",
        "handle": "@2b-lob",
        "channel_id": CID,
    }
    for kind in P.HOOKS:
        for p in P.build_posts(cfg, t, kind, P.PLATFORMS, milestone=1000):
            body = p.title if p.platform == "reddit" else p.text
            assert P.measure(body, P.PLATFORMS[p.platform][1]) == p.length <= p.limit, p.platform


def test_post_content(cfg):
    t = {
        "id": "trackDDDD04",
        "title": "Nattøy på Fløyen",
        "views": 150,
        "tags": ["psytrance", "dark techno"],
        "published_at": "2026-10-07T12:00:00Z",
        "handle": "@2b-lob",
        "channel_id": CID,
    }
    posts = {p.platform: p for p in P.build_posts(cfg, t, "release", P.PLATFORMS)}
    x = posts["x"].text
    assert "https://youtu.be/trackDDDD04" in x
    assert "https://fritog.lat/?utm_source=x&utm_medium=social&utm_campaign=nattoy-pa-floyen" in x
    assert x.endswith("#2bLob #NewMusic #Psytrance")
    assert "#Psytrance #DarkTechno" in posts["mastodon"].text
    assert posts["threads"].text.count("#") == 1
    assert "http" not in posts["instagram"].text and "fritog.lat" in posts["instagram"].text
    assert posts["reddit"].title == "2b-lob - Nattøy på Fløyen [Melodic Techno]"
    assert posts["reddit"].url == "https://youtu.be/trackDDDD04"
    assert "<https://fritog.lat/?utm_source=discord" in posts["discord"].text
    yt = posts["youtube"].text
    assert "utm_medium=video_description" in yt and "utm_content=trackDDDD04" in yt
    assert "https://www.youtube.com/@2b-lob?sub_confirmation=1" in yt


def test_hooks_are_deterministic(cfg):
    t = {"id": "trackAAAA01", "title": "Fjordlys", "views": 1000, "tags": [], "published_at": "2026-01-01T00:00:00Z"}
    a = P.build_posts(cfg, t, "milestone", ["x"], 1000)[0].text
    assert a == P.build_posts(cfg, t, "milestone", ["x"], 1000)[0].text
    assert "1k" in a


def test_quota_error_is_clear_and_hides_key(cfg, fake):
    fake.fail = [(403, "quotaExceeded")]
    with pytest.raises(P.YouTubeError) as e:
        P.YouTube(cfg).channel("@2b-lob")
    assert "quota" in str(e.value).lower() and "SECRET-KEY-123" not in str(e.value)


def test_bad_key_message_hides_key(cfg, fake):
    fake.fail = [(400, "badRequest SECRET-KEY-123")]
    with pytest.raises(P.YouTubeError) as e:
        P.YouTube(cfg).channel("@2b-lob")
    assert "SECRET-KEY-123" not in str(e.value) and "400" in str(e.value)


def test_retries_5xx(cfg, fake):
    fake.fail = [(503, "backendError"), (500, "backendError")]
    assert P.YouTube(cfg).channel("@2b-lob")["id"] == CID


def test_unknown_channel(cfg):
    with pytest.raises(P.YouTubeError, match="not found"):
        P.YouTube(cfg).channel("@nobody")


def test_missing_key(cfg):
    with pytest.raises(P.YouTubeError, match="YT_API_KEY"):
        P.YouTube(dataclasses.replace(cfg, api_key=""))


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("@2b-lob", {"forHandle": "@2b-lob"}),
        ("2b-lob", {"forHandle": "@2b-lob"}),
        ("https://www.youtube.com/@2b-lob/videos", {"forHandle": "@2b-lob"}),
        (CID, {"id": CID}),
        (f"https://youtube.com/channel/{CID}", {"id": CID}),
    ],
)
def test_channel_params(ref, expected):
    assert P.channel_params(ref) == expected


def test_helpers():
    assert P.iso_duration("PT1H2M3S") == 3723 and P.iso_duration("P0D") == 0 and P.iso_duration("") == 0
    assert P.parse_interval("30m") == 1800 and P.parse_interval("1d") == 86400
    with pytest.raises(ValueError):
        P.parse_interval("soon")
    assert P.slugify("Nattøy på Fløyen!") == "nattoy-pa-floyen"
    assert (
        P.utm("https://fritog.lat", "x", "social", "c")
        == "https://fritog.lat/?utm_source=x&utm_medium=social&utm_campaign=c"
    )
    assert P.video_id("https://youtu.be/trackAAAA01?si=abc") == "trackAAAA01"
    assert P.video_id("https://www.youtube.com/watch?v=trackAAAA01&t=3") == "trackAAAA01"
    assert [P.human(n) for n in (999, 1000, 1250, 999_999, 2_500_000)] == ["999", "1k", "1.2k", "1M", "2.5M"]
    assert P.hashtag("melodic techno") == "#MelodicTechno" and P.hashtag("a b c d") == ""
    assert P.measure("hi https://example.com/very/long", 23) == 3 + 23


def test_watch_cycle_promos_notifies_and_reports(cfg, fake, monkeypatch):
    sent = []
    monkeypatch.setattr(P, "notify", lambda c, title, body: sent.append((title, body)))
    db = P.open_db(cfg.db_path)

    P.run_cycle(cfg, db, now=T0)
    assert sent == []  # baseline: no flood of "new upload" alerts
    assert (cfg.db_path.parent / P.HEARTBEAT).exists()

    week_two(fake)
    P.run_cycle(cfg, db, now=LATER)
    titles = [t for t, _ in sent]
    assert titles[0] == "New upload: Nattøy på Fløyen"
    assert "https://youtu.be/trackDDDD04" in sent[0][1]
    assert titles[1] == "Regnbyen passed 1k views"
    assert titles[2] == "2b-lob weekly report"
    assert "Push this week: Kaldt Lys" in sent[2][1]  # highest engagement, not the newest
    assert "+15" in sent[2][1]
    promos = sorted(p.name for p in (cfg.out_dir / "promo").glob("*.md"))
    assert (
        len(promos) == 3
        and any("-release-" in p for p in promos)
        and any("-milestone-" in p for p in promos)
        and any("-spotlight-" in p for p in promos)
    )
    report = next((cfg.out_dir / "reports").glob("*-weekly.md")).read_text()
    assert "Top gainers (7d)" in report and "Regnbyen" in report and "Push this week" in report

    sent.clear()
    P.run_cycle(cfg, db, now=LATER + timedelta(hours=6))
    assert sent == []  # nothing new, report not due


def test_notify_ntfy_and_discord(cfg, fake):
    c = dataclasses.replace(cfg, ntfy_url=fake.url + "/promo", ntfy_token="tok", discord_webhook=fake.url + "/discord")
    P.notify(c, "Regnbyen passed 1k views — ø", "body ✓")
    (npath, nheaders, nbody), (dpath, _, dbody) = fake.posts
    assert npath.startswith("/promo?title=") and nheaders["Authorization"] == "Bearer tok" and nbody == "body ✓"
    assert dpath == "/discord" and json.loads(dbody)["content"].startswith("**Regnbyen passed 1k views")


def test_notify_failure_is_not_fatal(cfg):
    P.notify(
        dataclasses.replace(cfg, ntfy_url="http://127.0.0.1:9/x", discord_webhook="http://127.0.0.1:9/y"), "t", "b"
    )


def test_cli_end_to_end(cfg, tmp_path):
    def run(*args):
        return subprocess.run(
            [sys.executable, str(ROOT / "promoter.py"), *args],
            env=os.environ.copy(),
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

    for args in (["health"],):
        assert run(*args).returncode == 1  # no heartbeat yet
    for args in (
        ["sync"],
        ["top", "-n", "2"],
        ["top", "--by", "gain"],
        ["growth"],
        ["promo"],
        ["promo", "--video", "https://youtu.be/trackBBBB02", "--kind", "spotlight"],
        ["report", "--no-save"],
        ["watch", "--once"],
        ["health"],
    ):
        r = run(*args)
        assert r.returncode == 0, (args, r.stdout, r.stderr)

    top = json.loads(run("top", "--by", "engagement", "--json").stdout)
    assert top[0]["id"] == "trackCCCC03"  # 32/90 = 35.6%

    posts = json.loads(run("promo", "--top", "2", "--platforms", "x,bluesky", "--json", "--no-save").stdout)
    assert len(posts) == 2 and [p["platform"] for p in posts[0]["posts"]] == ["x", "bluesky"]
    assert posts[0]["file"] is None

    assert run("promo", "--platforms", "myspace").returncode != 0
    assert run("promo", "--video", "nope0000000").returncode != 0
    assert len(list((tmp_path / "data" / "out" / "promo").glob("*.md"))) == 2

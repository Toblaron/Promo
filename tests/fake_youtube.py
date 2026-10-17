"""Minimal fake of the YouTube Data API v3 endpoints promoter uses. Used by the tests;
also runnable standalone (`python tests/fake_youtube.py 8765`) for an offline dry run."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

CID = "UC" + "x" * 22
T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def video(vid, title, published, views, likes=None, comments=None, tags=()):
    stats = {"viewCount": str(views)}
    if likes is not None:
        stats["likeCount"] = str(likes)
    if comments is not None:
        stats["commentCount"] = str(comments)
    return {
        "id": vid,
        "snippet": {"title": title, "publishedAt": published.strftime("%Y-%m-%dT%H:%M:%SZ"), "tags": list(tags)},
        "statistics": stats,
        "contentDetails": {"duration": "PT5M30S"},
    }


class FakeYouTube:
    page_size = 2  # small pages so pagination is exercised

    def __init__(self):
        self.subs = 40
        self.videos: dict[str, dict] = {}
        self.fail: list[tuple[int, str]] = []
        self.calls: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict, str]] = []
        self.url = ""
        self._srv = None

    def add(self, v):  # newest first, like the real uploads playlist
        self.videos = {v["id"]: v, **self.videos}

    def set_views(self, vid, views):
        self.videos[vid]["statistics"]["viewCount"] = str(views)

    def seed(self):
        self.add(
            video("trackAAAA01", "Fjordlys", T0 - timedelta(days=200), 4200, 310, 22, ["melodic techno", "bergen"])
        )
        self.add(video("trackBBBB02", "Kaldt Lys", T0 - timedelta(days=60), 600, 80, 9, ["dark techno"]))
        self.add(video("trackCCCC03", "Regnbyen", T0 - timedelta(days=5), 90, 30, 2))
        return self

    def start(self, host="127.0.0.1", port=0):
        self._srv = ThreadingHTTPServer((host, port), _handler(self))
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_port}"
        return self

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def _handler(state: FakeYouTube):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, reason):
            self._send(code, {"error": {"code": code, "message": reason, "errors": [{"reason": reason}]}})

        def do_GET(self):
            u = urlsplit(self.path)
            q = dict(parse_qsl(u.query))
            ep = u.path.rsplit("/", 1)[-1]
            state.calls.append((ep, q))
            if state.fail:
                return self._error(*state.fail.pop(0))
            if ep == "channels":
                if q.get("forHandle") != "@2b-lob" and q.get("id") != CID:
                    return self._send(200, {"items": []})
                views = sum(int(v["statistics"]["viewCount"]) for v in state.videos.values())
                return self._send(
                    200,
                    {
                        "items": [
                            {
                                "id": CID,
                                "snippet": {"title": "2b-lob", "customUrl": "@2b-lob"},
                                "statistics": {
                                    "subscriberCount": str(state.subs),
                                    "viewCount": str(views),
                                    "videoCount": str(len(state.videos)),
                                    "hiddenSubscriberCount": False,
                                },
                                "contentDetails": {"relatedPlaylists": {"uploads": "UU" + CID[2:]}},
                            }
                        ]
                    },
                )
            if ep == "playlistItems":
                ids = list(state.videos)
                if not ids:
                    return self._error(404, "playlistNotFound")
                start = int(q.get("pageToken", 0))
                resp = {"items": [{"contentDetails": {"videoId": i}} for i in ids[start : start + state.page_size]]}
                if start + state.page_size < len(ids):
                    resp["nextPageToken"] = str(start + state.page_size)
                return self._send(200, resp)
            if ep == "videos":
                ids = q["id"].split(",")
                if len(ids) > 50:
                    return self._error(400, "tooManyIds")
                return self._send(200, {"items": [state.videos[i] for i in ids if i in state.videos]})
            self._error(404, "notFound")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            state.posts.append((self.path, dict(self.headers), self.rfile.read(n).decode()))
            self._send(200, {})

    return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    fake = FakeYouTube().seed()
    fake._srv = ThreadingHTTPServer(("0.0.0.0", port), _handler(fake))
    print(f"fake YouTube API on :{port}", flush=True)
    fake._srv.serve_forever()

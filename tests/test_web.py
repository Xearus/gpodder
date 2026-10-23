import importlib.util
import sqlite3
import time

import pytest

pytest.importorskip("fastapi", reason="Install the web extra to run native web tests")

from fastapi.testclient import TestClient
from werkzeug.wrappers import Response

from gpodder import util
from gpodder.web.app import create_app
from gpodder.web.engine import http_url

AUDIO = b"ID3" + b"test audio data" * 4096


def test_local_directories_do_not_require_gio(tmp_path):
    folder = tmp_path / "library" / "episodes"
    assert util.make_directory(folder)
    assert util.make_directory(str(folder))
    file = tmp_path / "not-a-directory"
    file.write_text("file")
    assert not util.make_directory(file)


def rss(base, count=2, media="/audio.mp3"):
    entries = "".join(
        f'<item><title>Episode {number}</title><guid isPermaLink="false">episode-{number}</guid>'
        f'<description>Story {number}</description><pubDate>Thu, {number:02} Oct 2026 12:00:00 GMT</pubDate>'
        f'<enclosure url="{base}{media}" length="{len(AUDIO)}" type="audio/mpeg"/></item>'
        for number in range(1, count + 1)
    )
    return (
        f'<rss version="2.0"><channel><title>Test stories</title><link>{base}</link>'
        f'<description>A local fixture</description>{entries}</channel></rss>'
    )


def eventually(get, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = get()
        if predicate(result):
            return result
        time.sleep(.05)
    pytest.fail(f"Timed out waiting for engine: {result}")


@pytest.fixture
def web(tmp_path, httpserver):
    httpserver.expect_request("/feed").respond_with_data(
        rss(httpserver.url_for("").rstrip("/")), content_type="application/rss+xml",
    )
    httpserver.expect_request("/audio.mp3").respond_with_data(AUDIO, content_type="audio/mpeg")
    app = create_app(tmp_path, poll_seconds=10)
    with TestClient(app) as client:
        yield client, app


def subscribe(client, httpserver, path="/feed"):
    response = client.post("/api/feeds", json={"url": httpserver.url_for(path)})
    assert response.status_code == 202
    feed_id = response.json()["id"]
    eventually(
        lambda: client.get("/api/feeds").json(),
        lambda feeds: any(feed["id"] == feed_id and feed["checked_at"] for feed in feeds),
    )
    feed = next(feed for feed in client.get("/api/feeds").json() if feed["id"] == feed_id)
    assert feed["error"] is None, feed
    return feed_id


def ready(client, episode_id):
    return eventually(
        lambda: client.get("/api/episodes").json(),
        lambda rows: next(row for row in rows if row["id"] == episode_id)["download_state"] == "ready",
    )


def test_native_headless_engine_and_feed(web, httpserver):
    client, app = web
    assert client.get("/health").json() == {"status": "ok", "engine": "gpodder", "desktop": False}
    assert client.get("/").status_code == 200
    assert (app.state.engine.home / "Database").is_file()
    assert app.state.engine.core.__class__.__module__ == "gpodder.core"
    assert importlib.util.find_spec("gi") is None
    assert importlib.util.find_spec("dbus") is None
    feed_id = subscribe(client, httpserver)
    episodes = client.get(f"/api/episodes?feed_id={feed_id}").json()
    assert len(episodes) == 2
    assert all(row["download_state"] == "not_downloaded" for row in episodes)
    assert all("cached_path" not in row for row in episodes)
    assert client.post("/api/feeds", json={"url": httpserver.url_for("/feed")}).json()["id"] == feed_id


def test_queue_download_audio_and_native_library(web, httpserver):
    client, app = web
    subscribe(client, httpserver)
    episode_id = client.get("/api/episodes").json()[0]["id"]
    assert client.post(f"/api/queue/{episode_id}").status_code == 202
    assert client.post(f"/api/queue/{episode_id}").status_code == 202
    ready(client, episode_id)
    queue = client.get("/api/queue").json()
    assert len(queue) == 1
    assert queue[0]["download_state"] == "ready"
    response = client.get(f"/api/episodes/{episode_id}/audio")
    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["content-type"] == "audio/mpeg"
    response = client.get(f"/api/episodes/{episode_id}/audio", headers={"Range": "bytes=0-2"})
    assert response.status_code == 206
    assert response.content == b"ID3"
    with sqlite3.connect(app.state.engine.home / "Database") as db:
        assert db.execute("SELECT COUNT(*) FROM podcast").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM episode WHERE state=1").fetchone()[0] == 1


def test_ordered_queue_and_invalid_reorders(web, httpserver):
    client, _ = web
    subscribe(client, httpserver)
    ids = [row["id"] for row in client.get("/api/episodes").json()]
    for episode_id in ids:
        client.post(f"/api/queue/{episode_id}").raise_for_status()
    assert client.put("/api/queue", json={"episode_ids": ids[::-1]}).status_code == 200
    assert [row["id"] for row in client.get("/api/queue").json()] == ids[::-1]
    assert client.put("/api/queue", json={"episode_ids": [ids[0], ids[0]]}).status_code == 409
    assert client.put("/api/queue", json={"episode_ids": []}).status_code == 409
    client.delete(f"/api/queue/{ids[0]}").raise_for_status()
    assert [row["id"] for row in client.get("/api/queue").json()] == [ids[1]]


def test_mark_listened_keeps_audio_and_excludes_from_queue(web, httpserver):
    client, _ = web
    subscribe(client, httpserver)
    episode_id = client.get("/api/episodes").json()[0]["id"]
    client.post(f"/api/queue/{episode_id}").raise_for_status()
    ready(client, episode_id)
    assert client.patch(f"/api/episodes/{episode_id}", json={"played": True}).status_code == 200
    assert client.get("/api/queue").json() == []
    assert client.post(f"/api/queue/{episode_id}").status_code == 409
    assert client.get(f"/api/episodes/{episode_id}/audio").content == AUDIO
    client.patch(f"/api/episodes/{episode_id}", json={"played": False}).raise_for_status()
    assert client.post(f"/api/queue/{episode_id}").status_code == 202


def test_feed_download_and_auto_download(web, httpserver):
    client, _ = web
    feed_id = subscribe(client, httpserver)
    assert client.patch(f"/api/feeds/{feed_id}", json={"auto_download": True}).status_code == 200
    eventually(
        lambda: client.get("/api/episodes").json(),
        lambda rows: len(rows) == 2 and all(row["download_state"] == "ready" for row in rows),
    )
    assert client.get("/api/queue").json() == []
    assert client.patch(f"/api/feeds/{feed_id}", json={"auto_download": False}).status_code == 200
    assert client.post(f"/api/feeds/{feed_id}/download").status_code == 202


def test_failed_download_retry(web, httpserver):
    client, _ = web
    httpserver.expect_request("/bad-feed").respond_with_data(
        rss(httpserver.url_for("").rstrip("/"), count=1, media="/retry.mp3"), content_type="application/rss+xml",
    )
    attempts = [0]

    def retry_audio(_request):
        attempts[0] += 1
        return Response(
            AUDIO if attempts[0] > 1 else b"forbidden",
            status=200 if attempts[0] > 1 else 403, content_type="audio/mpeg",
        )

    httpserver.expect_request("/retry.mp3").respond_with_handler(retry_audio)
    feed_id = subscribe(client, httpserver, "/bad-feed")
    episode_id = client.get(f"/api/episodes?feed_id={feed_id}").json()[0]["id"]
    client.post(f"/api/queue/{episode_id}").raise_for_status()
    rows = eventually(
        lambda: client.get("/api/episodes").json(),
        lambda rows: next(row for row in rows if row["id"] == episode_id)["download_state"] == "failed",
    )
    assert next(row for row in rows if row["id"] == episode_id)["error"]
    assert client.get("/health").status_code == 200
    assert client.post(f"/api/episodes/{episode_id}/download").status_code == 202
    ready(client, episode_id)
    assert client.get(f"/api/episodes/{episode_id}/audio").content == AUDIO


def test_api_responds_and_listened_mark_survives_slow_download(web, httpserver):
    client, _ = web

    def chunks():
        for _ in range(30):
            yield b"x" * 8192
            time.sleep(.05)

    httpserver.expect_request("/slow-feed").respond_with_data(
        rss(httpserver.url_for("").rstrip("/"), count=1, media="/slow.mp3"), content_type="application/rss+xml",
    )
    httpserver.expect_request("/slow.mp3").respond_with_handler(
        lambda _request: Response(chunks(), content_type="audio/mpeg"),
    )
    feed_id = subscribe(client, httpserver, "/slow-feed")
    episode_id = client.get(f"/api/episodes?feed_id={feed_id}").json()[0]["id"]
    client.post(f"/api/queue/{episode_id}").raise_for_status()
    eventually(
        lambda: client.get("/api/episodes").json(),
        lambda rows: next(row for row in rows if row["id"] == episode_id)["download_state"] == "downloading",
    )
    start = time.monotonic()
    assert client.get("/health").status_code == 200
    client.patch(f"/api/episodes/{episode_id}", json={"played": True}).raise_for_status()
    assert time.monotonic() - start < 1
    rows = ready(client, episode_id)
    assert next(row for row in rows if row["id"] == episode_id)["played"] == 1
    assert client.get("/api/queue").json() == []


def test_feed_errors_visible_and_engine_keeps_running(web, httpserver):
    client, _ = web
    httpserver.expect_request("/not-a-feed").respond_with_data("not a feed", content_type="text/plain")
    response = client.post("/api/feeds", json={"url": httpserver.url_for("/not-a-feed")})
    assert response.status_code == 202
    rows = eventually(
        lambda: client.get("/api/feeds").json(),
        lambda rows: any(row["error"] for row in rows),
    )
    assert rows[0]["error"]
    assert client.get("/health").status_code == 200
    subscribe(client, httpserver)


def test_due_feed_poll_and_missing_upstream_episodes(web, httpserver):
    client, app = web
    feed_id = subscribe(client, httpserver)
    ids = [row["id"] for row in client.get("/api/episodes").json()]
    client.post(f"/api/queue/{ids[0]}").raise_for_status()
    ready(client, ids[0])
    httpserver.clear()
    httpserver.expect_request("/feed").respond_with_data(
        rss(httpserver.url_for("").rstrip("/"), count=0), content_type="application/rss+xml",
    )
    app.state.store.execute("UPDATE feeds SET checked_at=0")
    rows = eventually(
        lambda: client.get("/api/episodes").json(),
        lambda rows: any(not row["available"] for row in rows),
    )
    assert len(rows) == 2
    assert client.get(f"/api/episodes/{ids[0]}/audio").content == AUDIO
    assert client.post(f"/api/queue/{ids[1]}").status_code == 409


def test_persistence_and_interrupted_job_recovery(tmp_path, httpserver):
    httpserver.expect_request("/feed").respond_with_data(
        rss(httpserver.url_for("").rstrip("/"), count=1), content_type="application/rss+xml",
    )
    httpserver.expect_request("/audio.mp3").respond_with_data(AUDIO, content_type="audio/mpeg")
    with TestClient(create_app(tmp_path)) as client:
        subscribe(client, httpserver)
        episode_id = client.get("/api/episodes").json()[0]["id"]
        client.post(f"/api/queue/{episode_id}").raise_for_status()
        ready(client, episode_id)
    with sqlite3.connect(tmp_path / "web.sqlite3") as db:
        db.execute("UPDATE episodes SET download_state='downloading'")
    with TestClient(create_app(tmp_path)) as client:
        ready(client, episode_id)
        assert client.get("/api/queue").json()[0]["id"] == episode_id
        assert client.get(f"/api/episodes/{episode_id}/audio").content == AUDIO
        client.patch(f"/api/episodes/{episode_id}", json={"played": True}).raise_for_status()
        eventually(
            lambda: client.get("/api/episodes").json(),
            lambda rows: rows[0]["mark_pending"] == 0,
        )
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/episodes").json()[0]["played"] == 1
        assert client.get("/api/queue").json() == []


def test_validation_and_missing_resources(web):
    client, _ = web
    for url in ["file:///etc/passwd", "ftp://example.test/feed", "http://user:pass@example.test/", "http://["]:
        assert client.post("/api/feeds", json={"url": url}).status_code == 400
    assert client.get("/api/episodes?limit=0").status_code == 422
    assert client.get("/api/episodes?feed_id=999").status_code == 404
    assert client.post("/api/queue/999").status_code == 404
    assert client.get("/api/episodes/999/audio").status_code == 404
    assert client.post("/api/feeds/999/download").status_code == 404
    for url in ["https://a:bad", "https://a:0", "http://a/\nx"]:
        with pytest.raises(ValueError):
            http_url(url)

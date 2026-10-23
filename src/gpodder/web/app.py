import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import gpodder

from .engine import Engine, http_url
from .store import Store


class FeedInput(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class FeedOptions(BaseModel):
    auto_download: bool


class EpisodeOptions(BaseModel):
    played: bool


class QueueOrder(BaseModel):
    episode_ids: List[int] = Field(max_length=10000)


def create_app(home=None, poll_seconds=None):
    home = Path(home or gpodder.home)
    poll_seconds = poll_seconds or int(os.environ.get("GPODDER_WEB_POLL_SECONDS", "300"))
    if poll_seconds < 10:
        raise ValueError("GPODDER_WEB_POLL_SECONDS must be at least 10")

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(home / "web.sqlite3")
        engine = Engine(app.state.store, home, poll_seconds)
        app.state.engine = engine
        engine.start()
        try:
            started = await asyncio.to_thread(engine.ready.wait, 30)
            if not started or not engine.initialized or not engine.is_alive():
                raise RuntimeError("gPodder engine failed to start; check server logs")
            yield
        finally:
            engine.stop()
            await asyncio.to_thread(engine.join, 75)
            if engine.is_alive():
                raise RuntimeError("gPodder engine did not stop within 75 seconds")

    app = FastAPI(title="gPodder Web", version="0.1.0", lifespan=lifespan)

    def store():
        return app.state.store

    def episode(episode_id):
        row = store().one("SELECT * FROM episodes WHERE id=?", (episode_id,))
        if row is None:
            raise HTTPException(404, "Episode not found")
        return row

    def feed(feed_id):
        row = store().one("SELECT * FROM feeds WHERE id=?", (feed_id,))
        if row is None:
            raise HTTPException(404, "Feed not found")
        return row

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health():
        if not app.state.engine.is_alive():
            raise HTTPException(503, "gPodder engine stopped; check server logs")
        store().one("SELECT 1")
        return {"status": "ok", "engine": "gpodder", "desktop": False}

    @app.get("/api/feeds")
    def feeds():
        return store().all("""
            SELECT feeds.*, (SELECT COUNT(*) FROM episodes WHERE feed_id=feeds.id) AS episodes
            FROM feeds ORDER BY title COLLATE NOCASE
        """)

    @app.post("/api/feeds", status_code=202)
    def subscribe(body: FeedInput):
        try:
            url = http_url(body.url.strip())
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        with store().connect() as db:
            db.execute("INSERT OR IGNORE INTO feeds(url, title) VALUES (?, ?)", (url, url))
            return dict(db.execute("SELECT * FROM feeds WHERE url=?", (url,)).fetchone())

    @app.post("/api/feeds/{feed_id}/refresh", status_code=202)
    def refresh(feed_id: int):
        feed(feed_id)
        store().execute("UPDATE feeds SET refresh_requested=1 WHERE id=?", (feed_id,))
        return {"status": "queued"}

    @app.patch("/api/feeds/{feed_id}")
    def feed_options(feed_id: int, body: FeedOptions):
        feed(feed_id)
        store().execute("UPDATE feeds SET auto_download=? WHERE id=?", (body.auto_download, feed_id))
        if body.auto_download:
            store().execute("""
                UPDATE episodes SET download_state='queued', error=NULL
                WHERE feed_id=? AND available=1 AND download_state IN ('not_downloaded', 'failed')
            """, (feed_id,))
        return feed(feed_id)

    @app.post("/api/feeds/{feed_id}/download", status_code=202)
    def download_all(feed_id: int):
        feed(feed_id)
        store().execute("""
            UPDATE episodes SET download_state='queued', error=NULL
            WHERE feed_id=? AND available=1 AND download_state NOT IN ('ready', 'downloading')
        """, (feed_id,))
        return {"status": "queued"}

    @app.get("/api/episodes")
    def episodes(
        feed_id: Optional[int] = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        if feed_id is not None:
            feed(feed_id)
        condition = "" if feed_id is None else "WHERE e.feed_id=?"
        args = () if feed_id is None else (feed_id,)
        rows = store().all(f"""
            SELECT e.*, f.title AS feed_title, q.position AS queue_position
            FROM episodes e JOIN feeds f ON e.feed_id=f.id
            LEFT JOIN queue q ON q.episode_id=e.id
            {condition} ORDER BY e.published DESC, e.id DESC LIMIT ? OFFSET ?
        """, (*args, limit, offset))
        for row in rows:
            row.pop("cached_path")
        return rows

    @app.patch("/api/episodes/{episode_id}")
    def mark(episode_id: int, body: EpisodeOptions):
        episode(episode_id)
        with store().connect() as db:
            db.execute(
                "UPDATE episodes SET played=?, mark_pending=1 WHERE id=?", (body.played, episode_id),
            )
            if body.played:
                db.execute("DELETE FROM queue WHERE episode_id=?", (episode_id,))
        return {"played": body.played}

    @app.post("/api/episodes/{episode_id}/download", status_code=202)
    def download_episode(episode_id: int):
        row = episode(episode_id)
        if not row["available"] and row["download_state"] != "ready":
            raise HTTPException(409, "Episode is no longer available in the feed")
        store().execute("""
            UPDATE episodes SET download_state='queued', error=NULL
            WHERE id=? AND download_state NOT IN ('ready', 'downloading')
        """, (episode_id,))
        return {"status": "queued"}

    @app.get("/api/queue")
    def queue():
        rows = store().all("""
            SELECT e.*, f.title AS feed_title, q.position AS queue_position
            FROM queue q JOIN episodes e ON e.id=q.episode_id JOIN feeds f ON f.id=e.feed_id
            ORDER BY q.position, e.id
        """)
        for row in rows:
            row.pop("cached_path")
        return rows

    @app.post("/api/queue/{episode_id}", status_code=202)
    def enqueue(episode_id: int):
        row = episode(episode_id)
        if row["played"]:
            raise HTTPException(409, "Mark the episode unlistened before adding it to Up next")
        if not row["available"] and row["download_state"] != "ready":
            raise HTTPException(409, "Episode is no longer available in the feed")
        try:
            store().queue_episode(episode_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "queued"}

    @app.delete("/api/queue/{episode_id}", status_code=204)
    def dequeue(episode_id: int):
        episode(episode_id)
        store().execute("DELETE FROM queue WHERE episode_id=?", (episode_id,))

    @app.put("/api/queue")
    def reorder(body: QueueOrder):
        try:
            store().reorder(body.episode_ids)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "reordered"}

    @app.get("/api/episodes/{episode_id}/audio")
    def audio(episode_id: int):
        row = episode(episode_id)
        if row["download_state"] != "ready" or not row["cached_path"]:
            raise HTTPException(409, "Episode has not finished downloading")
        path = Path(row["cached_path"]).resolve()
        download_root = Path(gpodder.downloads).resolve()
        if download_root not in path.parents or not path.is_file():
            raise HTTPException(404, "Cached audio is missing")
        audio_type = re.fullmatch(r"audio/[A-Za-z0-9.+-]+", row["mime_type"])
        return FileResponse(path, media_type=row["mime_type"] if audio_type else "application/octet-stream", headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline" if audio_type else "attachment",
        })

    return app


app = create_app()


def main():
    import uvicorn
    uvicorn.run("gpodder.web.app:app", host="0.0.0.0", port=8080)

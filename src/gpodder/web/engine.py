import logging
import os
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit

import podcastparser
import requests

import gpodder
from gpodder import core, download, feedcore

logger = logging.getLogger(__name__)
FETCH_ERRORS = (
    OSError, ValueError, URLError, requests.RequestException,
    feedcore.BadRequest, feedcore.InternalServerError, feedcore.WifiLogin,
    feedcore.Unsubscribe, feedcore.NotFound, feedcore.InvalidFeed,
    feedcore.UnknownStatusCode, feedcore.AuthenticationRequired,
    podcastparser.FeedParseError,
)


def http_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid feed URL") from error
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username is not None or parsed.password is not None or port == 0
        or any(ord(char) <= 32 for char in value)
    ):
        raise ValueError("Use an HTTP(S) URL without embedded credentials")
    return value


class Engine(threading.Thread):
    def __init__(self, store, home, poll_seconds=300):
        super().__init__(name="gpodder-engine", daemon=True)
        self.store = store
        self.home = home
        self.poll_seconds = poll_seconds
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.initialized = False
        self.active_task = None
        self.core = None

    def run(self):
        try:
            gpodder.set_home(str(self.home))
            gpodder.prefix = sys.prefix
            gpodder.ui.cli = True
            self.core = core.Core()
            self.store.execute(
                "UPDATE episodes SET download_state='queued' WHERE download_state='downloading'"
            )
            for feed in self.store.all("SELECT * FROM feeds"):
                podcast = self.podcast(feed)
                if podcast:
                    self.snapshot(feed, podcast)
            self.initialized = True
            self.ready.set()
            while not self.stopping.is_set():
                pending_mark = self.store.one(
                    "SELECT * FROM episodes WHERE mark_pending=1 ORDER BY id LIMIT 1"
                )
                if pending_mark:
                    self.mark_played(pending_mark)
                    continue
                feed = self.store.one("""
                    SELECT * FROM feeds
                    WHERE refresh_requested=1 OR checked_at IS NULL OR checked_at <= ?
                    ORDER BY refresh_requested DESC, COALESCE(checked_at, 0), id LIMIT 1
                """, (time.time() - self.poll_seconds,))
                if feed:
                    self.refresh(feed)
                    continue
                episode = self.store.one("""
                    SELECT episodes.* FROM episodes
                    LEFT JOIN queue ON queue.episode_id=episodes.id
                    WHERE download_state='queued'
                    ORDER BY queue.position IS NULL, queue.position, episodes.id LIMIT 1
                """)
                if episode:
                    self.cache(episode)
                    continue
                self.stopping.wait(.5)
        finally:
            self.ready.set()
            if self.core is not None:
                self.core.config.save()
                self.core.shutdown()

    def stop(self):
        self.stopping.set()
        if self.active_task:
            self.active_task.pause()

    def podcast(self, feed):
        return self.core.model.get_podcast(feed["native_url"] or feed["url"])

    def native_episode(self, row):
        feed = self.store.one("SELECT * FROM feeds WHERE id=?", (row["feed_id"],))
        podcast = self.podcast(feed)
        if podcast:
            return next((episode for episode in podcast.get_all_episodes() if episode.guid == row["guid"]), None)
        return None

    def snapshot(self, feed, podcast):
        with self.store.connect() as db:
            auto_download = db.execute("SELECT auto_download FROM feeds WHERE id=?", (feed["id"],)).fetchone()[0]
            db.execute("UPDATE episodes SET available=0 WHERE feed_id=?", (feed["id"],))
            for episode in podcast.get_all_episodes():
                filename = episode.local_filename(create=False, check_only=True)
                path = str(Path(filename).resolve()) if filename and os.path.isfile(filename) else None
                state = "ready" if path else ("queued" if auto_download else "not_downloaded")
                db.execute("""
                    INSERT INTO episodes
                        (feed_id, guid, title, description, published, size, mime_type,
                         played, cached_path, download_state, progress)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feed_id, guid) DO UPDATE SET
                        title=excluded.title, description=excluded.description,
                        published=excluded.published, size=excluded.size,
                        mime_type=excluded.mime_type, available=1,
                        played=CASE WHEN mark_pending=0 THEN excluded.played ELSE played END,
                        cached_path=excluded.cached_path,
                        download_state=CASE
                            WHEN excluded.cached_path IS NOT NULL THEN 'ready'
                            WHEN download_state IN ('ready', 'not_downloaded') THEN excluded.download_state
                            ELSE download_state END,
                        progress=CASE WHEN excluded.cached_path IS NOT NULL THEN 1 ELSE progress END
                """, (
                    feed["id"], episode.guid, episode.title,
                    episode.description or episode.description_html or "",
                    episode.published or 0, episode.file_size or 0, episode.mime_type or "",
                    int(not episode.is_new), path, state, int(path is not None),
                ))
            db.execute(
                "UPDATE feeds SET title=?, native_url=? WHERE id=?",
                (podcast.title, podcast.url, feed["id"]),
            )

    def refresh(self, feed):
        self.store.execute("UPDATE feeds SET refresh_requested=0 WHERE id=?", (feed["id"],))
        try:
            podcast = self.podcast(feed)
            if podcast is None:
                podcast = self.core.model.load_podcast(http_url(feed["url"]), max_episodes=0)
            else:
                podcast.update(max_episodes=0)
            self.snapshot(feed, podcast)
            self.core.db.commit()
            self.store.execute(
                "UPDATE feeds SET checked_at=?, error=NULL WHERE id=?", (time.time(), feed["id"]),
            )
        except FETCH_ERRORS as error:
            logger.exception("Feed update failed: %s", feed["url"])
            self.store.execute(
                "UPDATE feeds SET checked_at=?, error=? WHERE id=?",
                (time.time(), str(error), feed["id"]),
            )

    def mark_played(self, row):
        episode = self.native_episode(row)
        if episode:
            episode.mark(is_played=bool(row["played"]))
            self.core.db.commit()
        self.store.execute(
            "UPDATE episodes SET mark_pending=0 WHERE id=? AND played=?",
            (row["id"], row["played"]),
        )

    def cache(self, row):
        try:
            episode = self.native_episode(row)
            if episode is None:
                raise ValueError("Episode is no longer available in the feed")
            http_url(episode.url)
            filename = episode.local_filename(create=False, check_only=True)
            if filename and os.path.isfile(filename):
                self.snapshot(self.store.one("SELECT * FROM feeds WHERE id=?", (row["feed_id"],)), episode.channel)
                return
            task = episode.download_task
            if task is None:
                task = download.DownloadTask(episode, self.core.config)
            else:
                task.reuse()
            self.active_task = task
            task.add_progress_callback(lambda progress: self.store.execute(
                "UPDATE episodes SET progress=? WHERE id=?", (progress, row["id"]),
            ))
            self.store.execute(
                "UPDATE episodes SET download_state='downloading', error=NULL WHERE id=?", (row["id"],),
            )
            task.status = download.DownloadTask.DOWNLOADING
            task.run()
            if task.status == download.DownloadTask.PAUSED:
                self.store.execute(
                    "UPDATE episodes SET download_state='queued' WHERE id=?", (row["id"],),
                )
                return
            if task.status != download.DownloadTask.DONE:
                raise ValueError(task.error_message or "Download did not finish")
            # A download must not undo a manual listened mark.
            desired = self.store.one("SELECT played FROM episodes WHERE id=?", (row["id"],))
            episode.mark(is_played=bool(desired["played"]))
            self.core.db.commit()
            task.recycle()
            self.snapshot(self.store.one("SELECT * FROM feeds WHERE id=?", (row["feed_id"],)), episode.channel)
            self.store.execute("UPDATE episodes SET error=NULL WHERE id=?", (row["id"],))
        except FETCH_ERRORS as error:
            logger.exception("Episode download failed: %s", row["title"])
            self.store.execute(
                "UPDATE episodes SET download_state='failed', error=? WHERE id=?", (str(error), row["id"]),
            )
        finally:
            self.active_task = None

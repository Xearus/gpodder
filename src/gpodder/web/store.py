import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    """Web queue and read model; upstream objects stay on the engine thread."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    native_url TEXT,
                    title TEXT NOT NULL,
                    checked_at REAL,
                    error TEXT,
                    refresh_requested INTEGER NOT NULL DEFAULT 1,
                    auto_download INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_id INTEGER NOT NULL REFERENCES feeds(id),
                    guid TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    published REAL NOT NULL,
                    size INTEGER NOT NULL,
                    mime_type TEXT NOT NULL,
                    played INTEGER NOT NULL DEFAULT 0,
                    mark_pending INTEGER NOT NULL DEFAULT 0,
                    available INTEGER NOT NULL DEFAULT 1,
                    cached_path TEXT,
                    download_state TEXT NOT NULL DEFAULT 'not_downloaded',
                    progress REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    UNIQUE(feed_id, guid)
                );
                CREATE TABLE IF NOT EXISTS queue (
                    episode_id INTEGER PRIMARY KEY REFERENCES episodes(id),
                    position INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS episodes_by_feed ON episodes(feed_id, published DESC);
                CREATE INDEX IF NOT EXISTS pending_downloads ON episodes(download_state);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self, query, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, args)]

    def one(self, query, args=()):
        with self.connect() as db:
            row = db.execute(query, args).fetchone()
            return dict(row) if row else None

    def execute(self, query, args=()):
        with self.connect() as db:
            return db.execute(query, args).lastrowid

    def queue_episode(self, episode_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            episode = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if episode["played"]:
                raise ValueError("Mark the episode unlistened before adding it to Up next")
            if not episode["available"] and episode["download_state"] != "ready":
                raise ValueError("Episode is no longer available in the feed")
            db.execute("""
                INSERT OR IGNORE INTO queue(episode_id, position)
                VALUES (?, (SELECT COALESCE(MAX(position), 0) + 1 FROM queue))
            """, (episode_id,))
            db.execute("""
                UPDATE episodes SET download_state='queued', error=NULL
                WHERE id=? AND download_state NOT IN ('ready', 'downloading')
            """, (episode_id,))

    def reorder(self, ids):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = {row[0] for row in db.execute("SELECT episode_id FROM queue")}
            if len(ids) != len(set(ids)) or set(ids) != current:
                raise ValueError("Queue changed; reload before reordering")
            db.executemany(
                "UPDATE queue SET position=? WHERE episode_id=?",
                [(index, episode_id) for index, episode_id in enumerate(ids, 1)],
            )

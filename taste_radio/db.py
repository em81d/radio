"""SQLite storage: explicit-lyrics cache and play log."""
import sqlite3
import threading
import time

from .config import DB_FILE

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS explicit_cache (
    key TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,      -- 'clean' | 'explicit' | 'unknown'
    reason TEXT,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS plays (
    ts REAL,
    station TEXT,
    artist TEXT,
    title TEXT,
    score REAL,
    reason TEXT,
    event TEXT DEFAULT 'play'   -- 'play' | 'like' | 'skip'
);
"""


def conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            _conn.executescript(SCHEMA)
        return _conn


def get_explicit(key):
    c = conn()
    with _lock:
        row = c.execute(
            "SELECT verdict, reason FROM explicit_cache WHERE key = ?", (key,)
        ).fetchone()
    return row


def put_explicit(key, verdict, reason):
    c = conn()
    with _lock:
        c.execute(
            "INSERT OR REPLACE INTO explicit_cache VALUES (?, ?, ?, ?)",
            (key, verdict, reason, time.time()),
        )
        c.commit()


def log_play(station, artist, title, score, reason, event="play"):
    c = conn()
    with _lock:
        c.execute(
            "INSERT INTO plays VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), station, artist, title, score, reason, event),
        )
        c.commit()


def recent_plays(hours):
    c = conn()
    since = time.time() - hours * 3600
    with _lock:
        return c.execute(
            "SELECT artist, title FROM plays WHERE ts >= ? AND event = 'play'",
            (since,),
        ).fetchall()

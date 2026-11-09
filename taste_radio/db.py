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
CREATE TABLE IF NOT EXISTS lastfm_cache (
    method TEXT,
    artist_key TEXT,
    json TEXT,
    fetched_at REAL,
    PRIMARY KEY (method, artist_key)
);
CREATE TABLE IF NOT EXISTS station_stats (
    url TEXT PRIMARY KEY,
    name TEXT,
    songs_seen INTEGER DEFAULT 0,
    matches INTEGER DEFAULT 0,
    match_score_sum REAL DEFAULT 0,
    last_seen REAL
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


def get_lastfm(method, artist_key, max_age):
    c = conn()
    with _lock:
        row = c.execute("SELECT json, fetched_at FROM lastfm_cache WHERE method = ? AND artist_key = ?",
                        (method, artist_key)).fetchone()
    if row and time.time() - row[1] < max_age:
        return row[0]
    return None


def put_lastfm(method, artist_key, data_json):
    c = conn()
    with _lock:
        c.execute("INSERT OR REPLACE INTO lastfm_cache VALUES (?, ?, ?, ?)",
                  (method, artist_key, data_json, time.time()))
        c.commit()


def record_station_song(url, name, score):
    """Count one new song seen on a station, and whether it matched the profile."""
    c = conn()
    with _lock:
        c.execute(
            "INSERT INTO station_stats VALUES (?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET name = excluded.name, songs_seen = songs_seen + 1, "
            "matches = matches + excluded.matches, "
            "match_score_sum = match_score_sum + excluded.match_score_sum, "
            "last_seen = excluded.last_seen",
            (url, name, 1 if score > 0 else 0, score, time.time()))
        c.commit()


def station_stats():
    """{url: (name, songs_seen, matches, match_score_sum)}"""
    c = conn()
    with _lock:
        rows = c.execute("SELECT url, name, songs_seen, matches, match_score_sum "
                         "FROM station_stats").fetchall()
    return {r[0]: r[1:] for r in rows}


def recent_plays(hours):
    c = conn()
    since = time.time() - hours * 3600
    with _lock:
        return c.execute(
            "SELECT artist, title FROM plays WHERE ts >= ? AND event = 'play'",
            (since,),
        ).fetchall()

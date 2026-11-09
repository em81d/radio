"""Last.fm API: artist tags and similar artists, cached in SQLite for 30 days."""
import itertools
import json
import queue
import re
import threading
import time

import requests

from . import db
from .config import lastfm_key
from .util import normalize

API = "https://ws.audioscrobbler.com/2.0/"
UA = {"User-Agent": "TasteRadio/0.1"}
MAX_AGE = 30 * 24 * 3600
MIN_GAP = 0.25                 # <= 4 requests per second

_JUNK = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "my favorite",
    "albums i own", "love", "loved", "awesome", "beautiful", "amazing", "cool", "good",
    "all", "spotify", "under 2000 listeners", "male vocalists", "female vocalists",
    "male vocalist", "female vocalist", "vocalists", "check out", "to listen", "best",
    "american", "british", "uk", "usa", "us", "english", "canadian", "australian", "irish",
    "scottish", "welsh", "swedish", "norwegian", "danish", "german", "dutch", "new zealand",
    "icelandic", "finnish", "belgian", "music", "band", "bands", "artist", "musician",
}
_YEAR = re.compile(r"^(19|20)\d\d$")
_ALIASES = {"hiphop": "hip hop", "r and b": "rnb", "r&b": "rnb", "rhythm and blues": "rnb",
            "electronica": "electronic", "alt rock": "alternative rock", "alt country":
            "alternative country", "singer songwriters": "singer songwriter"}

_lock = threading.Lock()
_last_call = 0.0


class LastfmError(Exception):
    pass


def clean_tag(tag):
    t = re.sub(r"[-_/]+", " ", (tag or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    return _ALIASES.get(t, t)


def _keep_tag(tag, artist_key):
    return (tag and tag not in _JUNK and not _YEAR.match(tag)
            and normalize(tag) != artist_key and len(tag) <= 40)


def _call(method, **params):
    global _last_call
    params.update(method=method, api_key=lastfm_key(), format="json", autocorrect=1)
    for attempt in range(4):
        with _lock:
            wait = _last_call + MIN_GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
        try:
            r = requests.get(API, params=params, headers=UA, timeout=15)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == 3:
                raise LastfmError(str(e)) from e
            time.sleep(2 * (attempt + 1))
            continue
        err = data.get("error")
        if err == 6:                       # artist not found
            return None
        if err in (29, 11, 16):            # rate limited / temporarily unavailable
            time.sleep(5 * (attempt + 1))
            continue
        if err:
            raise LastfmError(f"{err}: {data.get('message')}")
        return data
    raise LastfmError("gave up after retries")


def _cached(method, artist, fetch):
    key = normalize(artist)
    hit = db.get_lastfm(method, key, MAX_AGE)
    if hit is not None:
        return json.loads(hit)
    result = fetch(key)
    db.put_lastfm(method, key, json.dumps(result))
    return result


def top_tags(artist):
    """[(tag, count 0-100)] with junk tags removed and spellings merged."""
    def fetch(key):
        data = _call("artist.gettoptags", artist=artist)
        tags = {}
        for t in (data or {}).get("toptags", {}).get("tag", []):
            name, count = clean_tag(t.get("name")), int(t.get("count", 0))
            if count > 0 and _keep_tag(name, key):
                tags[name] = max(tags.get(name, 0), count)
        return sorted(tags.items(), key=lambda x: -x[1])[:20]
    return [tuple(x) for x in _cached("tags", artist, fetch)]


def similar(artist, limit=100):
    """[(artist name, match 0-1)]"""
    def fetch(key):
        data = _call("artist.getsimilar", artist=artist, limit=limit)
        return [(a["name"], round(float(a.get("match", 0)), 4))
                for a in (data or {}).get("similarartists", {}).get("artist", [])]
    return [tuple(x) for x in _cached("similar", artist, fetch)]


def cached_tags(artist):
    """Tags if already cached, else None (never makes a network call)."""
    hit = db.get_lastfm("tags", normalize(artist), MAX_AGE)
    return [tuple(x) for x in json.loads(hit)] if hit is not None else None


class TagLookup:
    """Background tag lookups for artists seen on the radio (non-blocking)."""

    def __init__(self):
        self._q = queue.PriorityQueue()
        self._seq = itertools.count()
        self._queued = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def request(self, artist, priority=1):
        key = normalize(artist)
        with self._lock:
            if not key or key in self._queued:
                return
            self._queued.add(key)
        if cached_tags(artist) is None:
            self._q.put((priority, next(self._seq), artist))

    def pending(self):
        return self._q.qsize()

    def _worker(self):
        while True:
            _, _, artist = self._q.get()
            try:
                top_tags(artist)
            except Exception:  # noqa: BLE001 - offline etc.; allow a retry later
                with self._lock:
                    self._queued.discard(normalize(artist))

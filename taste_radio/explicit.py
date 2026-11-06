"""Explicit-lyrics filter. See 'Explicit filter' in TASTE_RADIO_PLAN.md.

Rule order (first match decides): clean station -> title hints -> cache ->
iTunes -> Deezer -> config `unknown_explicit`.

Run `python -m taste_radio.explicit --test` for the fixed test list.
"""
import itertools
import logging
import queue
import re
import threading
import time

import requests

from . import db
from .config import CONFIG, PKG_DIR
from .util import normalize, primary_artist, split_title, wildcard_match

ITUNES_URL = "https://itunes.apple.com/search"
DEEZER_URL = "https://api.deezer.com/search"
ITUNES_GAP = 3.0          # seconds between iTunes requests (~20/minute)
UA = {"User-Agent": "TasteRadio/0.1"}

ALLOWED, BLOCKED, PENDING = "allowed", "blocked", "pending"

_CLEAN_HINT = re.compile(
    r"[\(\[]\s*(clean|clean version|radio edit|radio version|edited|edit)\s*[\)\]]"
    r"|\bclean version\b|\bradio edit\b|\bedited\b", re.I)
_EXPLICIT_HINT = re.compile(r"[\(\[]\s*explicit( version)?\s*[\)\]]|\bexplicit version\b", re.I)

log = logging.getLogger("taste_radio.explicit")
if not log.handlers:
    _h = logging.FileHandler(PKG_DIR / "explicit.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


def cache_key(artist, title):
    return f"{primary_artist(artist)}|{normalize(title)}"


def _artist_matches(stream_artist, result_artist):
    a, b = primary_artist(stream_artist), primary_artist(result_artist)
    if not a or not b:
        return False
    if a == b:
        return True
    full_a, full_b = normalize(stream_artist), normalize(result_artist)
    return f" {a} " in f" {full_b} " or f" {b} " in f" {full_a} "


def _title_matches(stream_title, *result_titles):
    t = normalize(stream_title)
    return any(r and wildcard_match(t, normalize(r)) for r in result_titles)


def _search_term(artist, title):
    """Artist + title words, dropping masked words like 'S**t' that would spoil the search."""
    words = [w for w in f"{primary_artist(artist)} {normalize(title)}".split() if "*" not in w]
    return " ".join(words)


class RateLimited(Exception):
    pass


def lookup_itunes(artist, title):
    """('explicit'|'clean', reason) or None if iTunes has no matching song."""
    r = requests.get(ITUNES_URL, params={"term": _search_term(artist, title),
                                         "entity": "song", "limit": 10},
                     headers=UA, timeout=10)
    if r.status_code in (403, 429):
        raise RateLimited(f"iTunes HTTP {r.status_code}")
    r.raise_for_status()
    matches = [x for x in r.json().get("results", [])
               if _artist_matches(artist, x.get("artistName", ""))
               and _title_matches(title, x.get("trackName"), x.get("trackCensoredName"))]
    if not matches:
        return None
    flags = {x.get("trackExplicitness") for x in matches}
    if "explicit" in flags:
        return "explicit", "itunes: explicit"
    if "cleaned" in flags:
        return "explicit", "itunes: cleaned version exists"
    return "clean", f"itunes: notExplicit ({len(matches)} match{'es' if len(matches) > 1 else ''})"


def lookup_deezer(artist, title):
    q = f'artist:"{primary_artist(artist)}" track:"{normalize(title).replace("*", "")}"'
    r = requests.get(DEEZER_URL, params={"q": q, "limit": 10}, headers=UA, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RateLimited(f"Deezer: {data['error']}")
    matches = [x for x in data.get("data", [])
               if _artist_matches(artist, x.get("artist", {}).get("name", ""))
               and _title_matches(title, x.get("title"), x.get("title_short"))]
    if not matches:
        return None
    if any(x.get("explicit_lyrics") for x in matches):
        return "explicit", "deezer: explicit_lyrics"
    return "clean", "deezer: not explicit"


def lookup(artist, title):
    """Network lookup: ('clean'|'explicit'|'unknown', reason). Raises RateLimited."""
    res = lookup_itunes(artist, title)
    if res is None:
        res = lookup_deezer(artist, title)
    return res or ("unknown", "not found on iTunes or Deezer")


def _from_cache_verdict(verdict, reason):
    if verdict == "clean":
        return ALLOWED, reason
    if verdict == "explicit":
        return BLOCKED, reason
    rule = CONFIG["unknown_explicit"]
    return (ALLOWED if rule == "allow" else BLOCKED), f"unknown → {rule}ed"


class ExplicitChecker:
    """Decides whether a station's current song may be played.

    verdict() never blocks; songs that need a lookup return PENDING and are queued.
    """

    def __init__(self, start_worker=True):
        self._q = queue.PriorityQueue()
        self._queued = set()
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._logged = set()
        self._next_itunes = 0.0
        if start_worker:
            threading.Thread(target=self._worker, daemon=True).start()

    def _log_once(self, key, msg):
        if key not in self._logged:
            self._logged.add(key)
            log.info(msg)

    def verdict(self, station, stream_title):
        """(ALLOWED|BLOCKED|PENDING, reason) for this song on this station."""
        artist, title = split_title(stream_title)
        if station.get("plays_clean"):
            self._log_once((station["url"], stream_title),
                           f"ALLOWED  clean station  [{station['name']}] {stream_title}")
            return ALLOWED, "clean station"
        if _EXPLICIT_HINT.search(stream_title or ""):
            self._log_once(stream_title, f"BLOCKED  title hint  {stream_title}")
            return BLOCKED, "title hint: explicit"
        if _CLEAN_HINT.search(stream_title or ""):
            self._log_once(stream_title, f"ALLOWED  title hint  {stream_title}")
            return ALLOWED, "title hint: clean edit"
        if not artist:
            return BLOCKED, "no artist/title to check"
        row = db.get_explicit(cache_key(artist, title))
        if row:
            return _from_cache_verdict(*row)
        return PENDING, "not checked yet"

    def request_check(self, stream_title, priority=1):
        """Queue a lookup (lower priority number = sooner). Safe to call every tick."""
        artist, title = split_title(stream_title)
        if not artist:
            return
        key = cache_key(artist, title)
        with self._lock:
            if key in self._queued or db.get_explicit(key):
                return
            self._queued.add(key)
        self._q.put((priority, next(self._seq), key, artist, title))

    def check_now(self, artist, title):
        """Synchronous lookup + cache (used by --test). Returns (verdict, reason)."""
        key = cache_key(artist, title)
        row = db.get_explicit(key)
        if not row:
            wait = self._next_itunes - time.time()
            if wait > 0:
                time.sleep(wait)
            self._next_itunes = time.time() + ITUNES_GAP
            v, reason = lookup(artist, title)
            db.put_explicit(key, v, reason)
            log.info(f"{v.upper():8} {reason}  {artist} - {title}")
            row = (v, reason)
        return _from_cache_verdict(*row)

    def _worker(self):
        while True:
            prio, _, key, artist, title = self._q.get()
            try:
                self.check_now(artist, title)
            except RateLimited as e:
                log.info(f"rate limited ({e}); pausing 60 s")
                self._next_itunes = time.time() + 60
                self._q.put((prio, next(self._seq), key, artist, title))
                continue
            except Exception as e:  # noqa: BLE001 - network errors: drop; it'll be re-requested
                log.info(f"lookup failed for {artist} - {title}: {e}")
            with self._lock:
                self._queued.discard(key)


def _test():
    rule = CONFIG["unknown_explicit"]
    normal = {"name": "Test (internet station)", "url": "test://normal", "plays_clean": False}
    clean_st = {"name": "WTST 99.9", "url": "test://clean", "plays_clean": True}
    cases = [
        # (station, stream title, expected)
        (normal, "21 Savage & Metro Boomin - Rich N***a S**t", BLOCKED),
        (normal, "Kendrick Lamar - HUMBLE.", BLOCKED),
        (normal, "Grouplove - Francine", ALLOWED),
        (normal, "Some Artist - Some Song (Radio Edit)", ALLOWED),
        (normal, "Qwzxv Plonktrain - Marmalade Thermostat 9000",
         ALLOWED if rule == "allow" else BLOCKED),
        (clean_st, "21 Savage & Metro Boomin - Rich N***a S**t", ALLOWED),
    ]
    chk = ExplicitChecker(start_worker=False)
    ok = 0
    for station, st_title, expected in cases:
        v, reason = chk.verdict(station, st_title)
        if v == PENDING:
            v, reason = chk.check_now(*split_title(st_title))
        mark = "PASS" if v == expected else "FAIL"
        ok += v == expected
        print(f"  {mark}  {v:8} {st_title[:48]:48} [{station['name']}]  ({reason})")
    print(f"\n{ok}/{len(cases)} passed  (unknown_explicit = {rule!r})")


if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        _test()
    else:
        print(__doc__)

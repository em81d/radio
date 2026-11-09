"""Station pool: candidates from radio_stations.txt and Radio Browser, ranked by taste.

python -m taste_radio.stations                      show the current pool
python -m taste_radio.stations --refresh            rebuild it now
python -m taste_radio.stations --refresh --no-radio-browser
python -m taste_radio.stations --stats              learned match rate per station
"""
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest

import requests

from . import db
from .config import (CLEAN_STATIONS_FILE, CONFIG, DIRTY_STATIONS_FILE, POOL_FILE,
                     STATIONS_FILE)
from .lastfm import clean_tag
from .nowplaying import fetch_title, is_music
from .profile import load_profile
from .scorer import W_ARTIST, Scorer
from .util import normalize, split_title

POOL_MAX_AGE = 24 * 3600
SKIP_GENRES = {"CHRISTMAS"}
MIN_SONGS_FOR_RATE = 10        # songs seen before a station's match rate counts
# Used only without Last.fm enrichment
DEFAULT_TAGS = ["indie", "alternative", "pop", "rock", "folk", "hip hop", "rnb",
                "electronic", "chillout", "jazz", "soul", "country"]
RADIO_BROWSER_MIRRORS = ["https://de1.api.radio-browser.info", "https://nl1.api.radio-browser.info",
                         "https://at1.api.radio-browser.info", "https://de2.api.radio-browser.info",
                         "https://all.api.radio-browser.info"]

_P12_ENTRY = re.compile(r"^\s*(?:\d+\.|\*)\s+(.+?)(?:\s+\[listed as:.*\])?\s*$")
_SAMPLE = re.compile(r'\(e\.g\. "(.+)"\)')
_GROUP = re.compile(r"^---\s+(.+?)\s+---\s*$")
_CALL_LETTERS = re.compile(r"\b[KW][A-Z]{3}\b")
# HD2+ side channels, "24/7" web streams and -DB (internet-only) don't follow FCC rules
_SIDE_CHANNEL = re.compile(r"\bHD[2-9]\b|\b24\b|\b[KW][A-Z]{3}-DB\b", re.I)


def _station(name, url, part, genre, country="", tags=None, sample=None):
    return {"name": name, "url": url, "part": part, "genre": genre, "country": country,
            "tags": tags or [], "sample": sample, "plays_clean": False}


def load_from_file(path=STATIONS_FILE):
    """Stations from radio_stations.txt that sent song titles when tested."""
    stations, part, group, cur = [], 0, "", None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("PART "):
            part = int(line[5])
            continue
        g = _GROUP.match(line)
        if g:
            group = re.sub(r"\s*\(.*\)$", "", g.group(1)).strip()
            continue
        if part in (1, 2):
            m = _P12_ENTRY.match(line)
            if m and not line.strip().startswith(("stream:", "site:", "song info:")):
                cur = {"name": m.group(1).strip(), "url": None, "genre": group, "desc": True}
                continue
            if cur is None:
                continue
            s = line.strip()
            if cur.pop("desc", False):
                if part == 1:
                    cur["genre"] = s.split(" — ", 1)[0].strip() or "Mixed"
                cur["tags"] = [s.split(" — ", 1)[-1]]
            elif s.startswith("stream:"):
                cur["url"] = s.split(":", 1)[1].strip()
            elif s.startswith("song info:"):
                if s.startswith("song info: YES") and cur["url"]:
                    m = _SAMPLE.search(s)
                    stations.append(_station(cur["name"], cur["url"], part, cur["genre"],
                                             tags=cur.get("tags"), sample=m and m.group(1)))
                cur = None
        elif part == 3 and "|" in line and line.startswith("  "):
            fields = [f.strip() for f in line.split("|")]
            if len(fields) >= 5 and fields[-1].startswith("http") and group not in SKIP_GENRES:
                stations.append(_station(fields[0], fields[-1], 3, group.title(), fields[1],
                                         sample=fields[3].strip('"')))
    return stations


def search_radio_browser(tags=DEFAULT_TAGS, per_tag=25, min_bitrate=96):
    """Extra candidates from the Radio Browser directory, by genre tag."""
    out, seen = [], set()
    for tag in tags:
        results = None
        for base in RADIO_BROWSER_MIRRORS:      # the shared hostname drops connections often
            try:
                r = requests.get(
                    f"{base}/json/stations/bytag/{requests.utils.quote(tag)}",
                    params={"hidebroken": "true", "order": "votes", "reverse": "true",
                            "limit": per_tag * 2},
                    headers={"User-Agent": "TasteRadio/0.1"}, timeout=15)
                r.raise_for_status()
                results = r.json()
                break
            except (requests.RequestException, ValueError):
                continue
        if results is None:
            print(f"  radio-browser '{tag}' failed on all mirrors")
            continue
        n = 0
        for s in results:
            url = s.get("url_resolved") or s.get("url")
            if not url or url in seen or s.get("hls") or (s.get("bitrate") or 0) < min_bitrate:
                continue
            seen.add(url)
            tags = [t for t in (s.get("tags") or "").split(",") if t.strip()][:10]
            out.append(_station(s["name"].strip(), url, 4, tag.title(), s.get("country", ""),
                                tags=tags))
            n += 1
            if n >= per_tag:
                break
    return out


def _read_list(path):
    if not path.exists():
        return set()
    items = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            items.add(line if line.startswith("http") else normalize(line))
    return items


def plays_clean(station, clean=None, dirty=None):
    """True if the station is expected to play radio edits (see TASTE_RADIO_PLAN.md)."""
    clean = _read_list(CLEAN_STATIONS_FILE) if clean is None else clean
    dirty = _read_list(DIRTY_STATIONS_FILE) if dirty is None else dirty
    keys = {station["url"], normalize(station["name"])}
    if keys & dirty:
        return False
    if keys & clean:
        return True
    name = station["name"]
    if not _CALL_LETTERS.search(name) or _SIDE_CHANNEL.search(name):
        return False
    country = station.get("country", "")
    # Parts 1-2 are hand-picked and mostly US; part 3+ must say US explicitly
    return "United States" in country or (station["part"] in (1, 2) and not country)


def _interleave(stations):
    """Part 1 first, then round-robin across the genres of everything else."""
    first = [s for s in stations if s["part"] == 1]
    groups = {}
    for s in stations:
        if s["part"] >= 2:
            groups.setdefault(s["genre"], []).append(s)
    rr = [s for row in zip_longest(*groups.values()) for s in row if s]
    return first + rr


def _check(station):
    try:
        title = fetch_title(station["url"])
    except Exception:  # noqa: BLE001
        return station, None
    return station, title


_QUALITY = re.compile(r"\b(\d{2,3}\s?k(bps)?|aac\+?|mp3|ogg|hq|lq|hd|low|high|mobile|stream)\b", re.I)


def _station_key(name):
    """'SomaFM Indie Pop Rocks! (128k AAC)' -> 'somafm indie pop rocks'."""
    return "name:" + normalize(_QUALITY.sub(" ", name))


def station_tags(station):
    """Genre tags for a station, from its genre label and Radio Browser tags."""
    text = ",".join([station.get("genre", "")] + station.get("tags", []))
    parts = re.split(r"\s*(?:,|/|&|\band\b|\+|;)\s*", text.lower())
    return {clean_tag(p) for p in parts if p.strip()}


class Relevance:
    """How well a station fits your taste: 0.5 genre + 0.3 songs + 0.2 learned match rate."""

    def __init__(self):
        self.profile = load_profile()
        self.scorer = Scorer(self.profile, None)
        self.stats = db.station_stats()

    def genre(self, station):
        g = self.profile.genres
        return max((g.get(t, 0) for t in station_tags(station)), default=0)

    def song(self, title):
        artist, song = split_title(title or "")
        if not artist:
            return 0.0
        return min(1.0, self.scorer.taste(artist, song)[0] / W_ARTIST)

    def learned(self, station):
        """(match rate 0-1, songs seen) from past listening, normalized so 30% matches = 1."""
        st = self.stats.get(station["url"])
        if not st or st[1] < MIN_SONGS_FOR_RATE:
            return None, (st[1] if st else 0)
        return min(1.0, (st[2] / st[1]) / 0.3), st[1]

    def score(self, station, live_title=None):
        rate, _ = self.learned(station)
        songs = max(self.song(station.get("sample")), self.song(live_title))
        return 0.5 * self.genre(station) + 0.3 * songs + 0.2 * (rate if rate is not None else 0.25)


def build_pool(n=None, use_radio_browser=True, verbose=True):
    n = n or CONFIG["pool_size"]
    rel = Relevance()
    candidates = load_from_file()
    if not rel.profile.genres:
        # no Last.fm enrichment yet: fall back to a varied pool
        return _build_varied(candidates, n, verbose)

    top_genres = list(rel.profile.genres)[:15]
    if use_radio_browser:
        if verbose:
            print(f"  searching Radio Browser for: {', '.join(top_genres)}")
        candidates += search_radio_browser(top_genres)
    # one entry per station: the same station often has several streams (128k AAC, 320k...)
    seen, unique = set(), []
    for s in candidates:
        key = _station_key(s["name"])
        if s["url"] not in seen and key not in seen:
            seen.update((s["url"], key))
            unique.append(s)

    # Drop stations that have proven to be a poor fit; rank the rest before checking
    def proven_bad(s):
        rate, seen_n = rel.learned(s)
        return rate is not None and seen_n >= 30 and rate < 0.1
    unique = [s for s in unique if not proven_bad(s)]
    unique.sort(key=lambda s: -rel.score(s))
    to_check = unique[:n * 3]

    clean, dirty = _read_list(CLEAN_STATIONS_FILE), _read_list(DIRTY_STATIONS_FILE)
    live = []
    with ThreadPoolExecutor(max_workers=24) as ex:
        for s, title in ex.map(_check, to_check):
            if title and is_music(title, s["name"]):
                s["plays_clean"] = plays_clean(s, clean, dirty)
                s["relevance"] = round(rel.score(s, title), 3)
                live.append(s)
    if verbose:
        print(f"  checked {len(to_check)} of {len(unique)} candidates, {len(live)} working")

    live.sort(key=lambda s: -s["relevance"])
    n_explore = max(1, n // 10)
    pool = live[:n - n_explore]
    rest = [s for s in live[n - n_explore:] if rel.genre(s) > 0]
    pool += random.sample(rest, min(n_explore, len(rest)))
    _save(pool)
    return pool


def _build_varied(candidates, n, verbose):
    seen, ordered = set(), []
    for s in _interleave(candidates):
        if s["url"] not in seen:
            seen.add(s["url"])
            ordered.append(s)
    clean, dirty = _read_list(CLEAN_STATIONS_FILE), _read_list(DIRTY_STATIONS_FILE)
    pool, tried = [], 0
    with ThreadPoolExecutor(max_workers=24) as ex:
        while len(pool) < n and tried < len(ordered):
            batch = ordered[tried:tried + 32]
            tried += len(batch)
            for s, title in ex.map(_check, batch):
                if title and is_music(title, s["name"]) and len(pool) < n:
                    s["plays_clean"] = plays_clean(s, clean, dirty)
                    pool.append(s)
            if verbose:
                print(f"  checked {tried} stations, {len(pool)} usable")
    _save(pool)
    return pool


def _save(pool):
    POOL_FILE.write_text(json.dumps({"built_at": time.time(), "stations": pool}, indent=1),
                         encoding="utf-8")


def load_pool():
    """The cached pool, rebuilt if missing or older than 24 hours."""
    if POOL_FILE.exists():
        data = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        if time.time() - data["built_at"] < POOL_MAX_AGE and data["stations"]:
            # re-apply the clean/dirty lists in case they were edited
            clean, dirty = _read_list(CLEAN_STATIONS_FILE), _read_list(DIRTY_STATIONS_FILE)
            for s in data["stations"]:
                s["plays_clean"] = plays_clean(s, clean, dirty)
            return data["stations"]
    print("Building station pool (this takes about a minute)...")
    return build_pool()


def main():
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--stats" in sys.argv:
        return show_stats()
    if "--refresh" in sys.argv:
        pool = build_pool(use_radio_browser="--no-radio-browser" not in sys.argv)
    else:
        pool = load_pool()
    genres = {}
    for s in pool:
        genres[s["genre"]] = genres.get(s["genre"], 0) + 1
    print(f"\nPool: {len(pool)} stations")
    print("Genres: " + ", ".join(f"{g} {c}" for g, c in sorted(genres.items(), key=lambda x: -x[1])))
    clean = [s["name"] for s in pool if s["plays_clean"]]
    print(f"plays_clean ({len(clean)}): " + (", ".join(clean) or "none"))
    if pool and "relevance" in pool[0]:
        print("\nMost relevant:")
        for s in sorted(pool, key=lambda s: -s.get("relevance", 0))[:15]:
            print(f"  {s['relevance']:.2f}  {s['name'][:40]:40}  {s['genre']}")


def show_stats():
    stats = db.station_stats()
    rows = [(m / n, n, m, name) for url, (name, n, m, _) in stats.items() if n]
    if not rows:
        print("No station stats yet: they fill in while the radio runs.")
        return
    rows.sort(reverse=True)
    print(f"{'match':>6} {'songs':>6}  station   (rate counts after {MIN_SONGS_FOR_RATE} songs)")
    for rate, n, m, name in rows:
        flag = "" if n >= MIN_SONGS_FOR_RATE else "  (too few songs yet)"
        print(f"{rate:6.0%} {n:6}  {name}{flag}")


if __name__ == "__main__":
    main()

"""Station candidates: radio_stations.txt, plus optional Radio Browser search.

Run `python -m taste_radio.stations --refresh` to rebuild and summarize the pool.
Add `--radio-browser` to also pull extra stations from radio-browser.info.
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest

import requests

from .config import (CLEAN_STATIONS_FILE, CONFIG, DIRTY_STATIONS_FILE, POOL_FILE,
                     STATIONS_FILE)
from .nowplaying import fetch_title, is_music
from .util import normalize

POOL_MAX_AGE = 24 * 3600
SKIP_GENRES = {"CHRISTMAS"}
# Hard-coded until step 4 replaces it with your Last.fm genre tags
DEFAULT_TAGS = ["indie", "alternative", "pop", "rock", "folk", "hip hop", "rnb",
                "electronic", "chillout", "jazz", "soul", "country"]
RADIO_BROWSER = "https://all.api.radio-browser.info/json"

_P12_ENTRY = re.compile(r"^\s*(?:\d+\.|\*)\s+(.+?)(?:\s+\[listed as:.*\])?\s*$")
_GROUP = re.compile(r"^---\s+(.+?)\s+---\s*$")
_CALL_LETTERS = re.compile(r"\b[KW][A-Z]{3}\b")
# HD2+ side channels, "24/7" web streams and -DB (internet-only) don't follow FCC rules
_SIDE_CHANNEL = re.compile(r"\bHD[2-9]\b|\b24\b|\b[KW][A-Z]{3}-DB\b", re.I)


def _station(name, url, part, genre, country=""):
    return {"name": name, "url": url, "part": part, "genre": genre,
            "country": country, "plays_clean": False}


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
            if cur.pop("desc", False) and part == 1:
                cur["genre"] = s.split(" — ", 1)[0].strip() or "Mixed"
            elif s.startswith("stream:"):
                cur["url"] = s.split(":", 1)[1].strip()
            elif s.startswith("song info:"):
                if s.startswith("song info: YES") and cur["url"]:
                    stations.append(_station(cur["name"], cur["url"], part, cur["genre"]))
                cur = None
        elif part == 3 and "|" in line and line.startswith("  "):
            fields = [f.strip() for f in line.split("|")]
            if len(fields) >= 5 and fields[-1].startswith("http") and group not in SKIP_GENRES:
                stations.append(_station(fields[0], fields[-1], 3, group.title(), fields[1]))
    return stations


def search_radio_browser(tags=DEFAULT_TAGS, per_tag=25, min_bitrate=96):
    """Extra candidates from the Radio Browser directory, by genre tag."""
    out, seen = [], set()
    for tag in tags:
        try:
            r = requests.get(
                f"{RADIO_BROWSER}/stations/bytag/{requests.utils.quote(tag)}",
                params={"hidebroken": "true", "order": "votes", "reverse": "true",
                        "limit": per_tag * 2},
                headers={"User-Agent": "TasteRadio/0.1"}, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  radio-browser '{tag}' failed: {e}")
            continue
        n = 0
        for s in r.json():
            url = s.get("url_resolved") or s.get("url")
            if not url or url in seen or s.get("hls") or (s.get("bitrate") or 0) < min_bitrate:
                continue
            seen.add(url)
            out.append(_station(s["name"].strip(), url, 4, tag.title(), s.get("country", "")))
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


def build_pool(n=None, use_radio_browser=False, verbose=True):
    n = n or CONFIG["pool_size"]
    candidates = load_from_file()
    if use_radio_browser:
        candidates += search_radio_browser()
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
    POOL_FILE.write_text(json.dumps({"built_at": time.time(), "stations": pool}, indent=1),
                         encoding="utf-8")
    return pool


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

    if "--refresh" in sys.argv:
        pool = build_pool(use_radio_browser="--radio-browser" in sys.argv)
    else:
        pool = load_pool()
    genres = {}
    for s in pool:
        genres[s["genre"]] = genres.get(s["genre"], 0) + 1
    print(f"\nPool: {len(pool)} stations")
    print("Genres: " + ", ".join(f"{g} {c}" for g, c in sorted(genres.items(), key=lambda x: -x[1])))
    clean = [s["name"] for s in pool if s["plays_clean"]]
    print(f"plays_clean ({len(clean)}): " + (", ".join(clean) or "none"))


if __name__ == "__main__":
    main()

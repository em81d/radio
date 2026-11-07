"""Build taste_profile.json from the Spotify export in raw_export/.

python -m taste_radio.profile_builder            build + show top 30 artists, top 20 tracks
python -m taste_radio.profile_builder --top 100  show more

Artist weight (each signal log-scaled to 0-1):
  0.25 listening  hours in the 1-year history (plays < 30 s count as skips)
  0.30 playlists  distinct playlists containing the artist
  0.30 liked      Liked Songs by the artist
  0.15 wrapped    rank-weighted appearances in Wrapped playlists, newer years count more
  +0.05 recency   share of the artist's listening in the last 90 days
History covers only one year, so the long-term signals (playlists, liked, wrapped)
carry more weight than listening.
"""
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .config import ROOT
from .profile import PROFILE_JSON
from .util import clean_title, normalize

EXPORT_DIR = ROOT / "raw_export"
IGNORED_PLAYLISTS = {"discover weekly archive"}
WRAPPED = re.compile(r"your top songs (\d{4})|(\d{4}) wrapped|wrapped (\d{4})", re.I)

W_LISTEN, W_PLAYLISTS, W_LIKED, W_WRAPPED, W_RECENCY = 0.25, 0.30, 0.30, 0.15, 0.05
SKIP_MS = 30_000            # plays shorter than this count as skips
FULL_PLAY_MS = 60_000       # plays at least this long count toward "songs you love"
SKIP_PENALTY_HOURS = 0.01   # each skip subtracts ~36 s of listening
RECENT_DAYS = 90
MIN_WEIGHT = 0.05


def _load(name):
    return json.loads((EXPORT_DIR / name).read_text(encoding="utf-8"))


def _logscale(counter):
    top = max(counter.values(), default=0)
    if top <= 0:
        return {}
    return {k: math.log1p(max(v, 0)) / math.log1p(top) for k, v in counter.items()}


def _wrapped_year(name):
    m = WRAPPED.search(name)
    return int(next(g for g in m.groups() if g)) if m else None


class Builder:
    def __init__(self):
        self.display = defaultdict(Counter)          # norm artist -> display name counts
        self.track_display = {}                      # (artist, title) norm -> (artist, title)
        self.hours, self.skips, self.recent_ms, self.total_ms = (Counter() for _ in range(4))
        self.playlists, self.liked, self.wrapped = Counter(), Counter(), Counter()
        self.full_plays, self.track_playlists = Counter(), Counter()
        self.track_liked, self.track_wrapped = set(), {}
        self.stats = {}

    def _artist(self, name):
        key = normalize(name)
        if key:
            self.display[key][name] += 1
        return key

    def _track(self, artist, title):
        a, t = self._artist(artist), normalize(clean_title(title))
        if a and t:
            self.track_display.setdefault((a, t), (artist, clean_title(title)))
            return a, t
        return None

    def read_history(self):
        rows = _load("StreamingHistory_music_0.json")
        end = max(datetime.strptime(r["endTime"], "%Y-%m-%d %H:%M") for r in rows)
        cutoff = end - timedelta(days=RECENT_DAYS)
        for r in rows:
            a = self._artist(r["artistName"])
            if not a:
                continue
            ms = r["msPlayed"]
            if ms < SKIP_MS:
                self.skips[a] += 1
                continue
            self.total_ms[a] += ms
            if datetime.strptime(r["endTime"], "%Y-%m-%d %H:%M") >= cutoff:
                self.recent_ms[a] += ms
            if ms >= FULL_PLAY_MS:
                key = self._track(r["artistName"], r["trackName"])
                if key:
                    self.full_plays[key] += 1
        for a, ms in self.total_ms.items():
            self.hours[a] = ms / 3.6e6
        self.stats.update(history_rows=len(rows), history_hours=round(sum(self.hours.values()), 1),
                          history_from=min(r["endTime"] for r in rows)[:10],
                          history_to=end.strftime("%Y-%m-%d"))

    def read_playlists(self):
        n_pl = n_wrapped = n_ignored = 0
        for path in sorted(EXPORT_DIR.glob("Playlist*.json")):
            for pl in _load(path.name).get("playlists", []):
                name = pl.get("name", "")
                if name.strip().lower() in IGNORED_PLAYLISTS:
                    n_ignored += 1
                    continue
                tracks = [it["track"] for it in pl.get("items", []) if it.get("track")]
                year = _wrapped_year(name)
                if year:
                    n_wrapped += 1
                    year_factor = min(1.0, 0.6 + 0.1 * (year - 2020))
                    for rank, t in enumerate(tracks):
                        pos = 1 - rank / max(len(tracks), 1)       # 1.0 for #1, ~0 for last
                        a = self._artist(t["artistName"])
                        self.wrapped[a] += pos * year_factor
                        key = self._track(t["artistName"], t["trackName"])
                        if key:
                            w = 0.7 + 0.3 * pos
                            self.track_wrapped[key] = max(self.track_wrapped.get(key, 0), w)
                    continue
                n_pl += 1
                for a in {self._artist(t["artistName"]) for t in tracks} - {""}:
                    self.playlists[a] += 1
                for key in {self._track(t["artistName"], t["trackName"]) for t in tracks} - {None}:
                    self.track_playlists[key] += 1
        self.stats.update(playlists=n_pl, wrapped_playlists=n_wrapped, ignored_playlists=n_ignored)

    def read_library(self):
        tracks = _load("YourLibrary.json").get("tracks", [])
        for t in tracks:
            a = self._artist(t["artist"])
            self.liked[a] += 1
            key = self._track(t["artist"], t["track"])
            if key:
                self.track_liked.add(key)
        self.stats.update(liked_songs=len(tracks))

    def build(self):
        self.read_history()
        self.read_playlists()
        self.read_library()

        listen = Counter({a: max(0.0, h - SKIP_PENALTY_HOURS * self.skips[a])
                          for a, h in self.hours.items()})
        L, P, K, W = (_logscale(c) for c in (listen, self.playlists, self.liked, self.wrapped))
        raw, details = {}, {}
        for a in set(L) | set(P) | set(K) | set(W):
            share = self.recent_ms[a] / self.total_ms[a] if self.total_ms[a] else 0
            recency = W_RECENCY * share if self.hours[a] >= 0.5 else 0.0
            raw[a] = (W_LISTEN * L.get(a, 0) + W_PLAYLISTS * P.get(a, 0) +
                      W_LIKED * K.get(a, 0) + W_WRAPPED * W.get(a, 0) + recency)
            details[a] = {"hours": round(self.hours[a], 2), "skips": self.skips[a],
                          "playlists": self.playlists[a], "liked": self.liked[a],
                          "wrapped": round(self.wrapped[a], 2),
                          "recent_share": round(share, 2)}
        top = max(raw.values())
        artists, artist_details = {}, {}
        for a, s in sorted(raw.items(), key=lambda x: -x[1]):
            w = s / top
            if w < MIN_WEIGHT:
                continue
            name = self.display[a].most_common(1)[0][0]
            artists[name] = round(w, 4)
            artist_details[name] = details[a]

        tracks = {}
        for key in set(self.track_wrapped) | self.track_liked | set(self.full_plays) | set(self.track_playlists):
            w = 0.0
            w = max(w, self.track_wrapped.get(key, 0))
            if key in self.track_liked:
                w = max(w, 0.6)
            if self.full_plays[key] >= 3:
                w = max(w, min(0.8, 0.4 + 0.1 * self.full_plays[key]))
            if self.track_playlists[key] >= 3:
                w = max(w, 0.5)
            if w > 0:
                tracks[key] = w
        track_list = [{"artist": self.track_display[k][0], "title": self.track_display[k][1],
                       "weight": round(w, 3)}
                      for k, w in sorted(tracks.items(), key=lambda x: -x[1])]

        self.stats.update(artists_kept=len(artists), artists_seen=len(raw), tracks=len(track_list))
        return {"built_at": time.strftime("%Y-%m-%d %H:%M"), "source": self.stats,
                "weights": {"listening": W_LISTEN, "playlists": W_PLAYLISTS, "liked": W_LIKED,
                            "wrapped": W_WRAPPED, "recency": W_RECENCY},
                "artists": artists, "tracks": track_list, "artist_details": artist_details}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not EXPORT_DIR.exists():
        sys.exit("raw_export/ not found: extract my_spotify_data.zip there first.")
    n = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 30
    profile = Builder().build()
    PROFILE_JSON.write_text(json.dumps(profile, indent=1, ensure_ascii=False), encoding="utf-8")

    s = profile["source"]
    print(f"History {s['history_from']} to {s['history_to']}: {s['history_rows']} plays, "
          f"{s['history_hours']} h | {s['playlists']} playlists + {s['wrapped_playlists']} Wrapped "
          f"({s['ignored_playlists']} ignored) | {s['liked_songs']} liked")
    print(f"Kept {s['artists_kept']} of {s['artists_seen']} artists, {s['tracks']} favorite tracks\n")
    print(f"{'#':>3}  {'weight':>6}  {'artist':30} {'hours':>6} {'lists':>5} {'liked':>5} {'wrapd':>5} {'recent':>6}")
    for i, (name, w) in enumerate(list(profile["artists"].items())[:n], 1):
        d = profile["artist_details"][name]
        print(f"{i:>3}  {w:6.3f}  {name[:30]:30} {d['hours']:6.1f} {d['playlists']:5} "
              f"{d['liked']:5} {d['wrapped']:5.1f} {d['recent_share']:6.0%}")
    print("\nTop tracks:")
    for t in profile["tracks"][:min(n, 20)]:
        print(f"  {t['weight']:.2f}  {t['artist']} - {t['title']}")
    print(f"\nSaved {PROFILE_JSON.name}")


if __name__ == "__main__":
    main()

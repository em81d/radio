"""Taste profile: artist and track weights.

load_profile() reads taste_profile.json (built by profile_builder.py) and merges
seed_artists.txt on top as manual overrides. Without the JSON, the seed file alone is used.
"""
import json
import re

from .config import ROOT, SEED_ARTISTS_FILE
from .util import clean_title, normalize, primary_artist, split_title

PROFILE_JSON = ROOT / "taste_profile.json"


class Profile:
    def __init__(self, artists=None, tracks=None, source=""):
        self.artists = artists or {}   # normalized artist -> (weight 0-1, display name)
        self.tracks = tracks or {}     # (normalized artist, normalized title) -> weight
        self.source = source

    def _credited(self, artist):
        """Every artist credited in a stream's artist field, normalized."""
        parts = re.split(r"\s*(?:,|&|\band\b|\bx\b|/|\bwith\b|\bvs\.?|\bfeat\.?|\bft\.?|"
                         r"\bfeaturing)\s*", artist or "", flags=re.I)
        names = [normalize(artist)] + [normalize(p) for p in parts]
        return [n for n in dict.fromkeys(names) if n]

    def artist_weight(self, artist):
        """(weight, display name) of the best-matching profile artist, or (0, None)."""
        best = (0.0, None)
        for name in self._credited(artist):
            if name in self.artists and self.artists[name][0] > best[0]:
                best = self.artists[name]
        return best

    def track_bonus(self, artist, title):
        """Weight if this exact song is in the profile, else 0."""
        t = normalize(clean_title(title))
        for name in self._credited(artist):
            if (name, t) in self.tracks:
                return self.tracks[(name, t)]
        return 0.0

    def __len__(self):
        return len(self.artists)


def load_seed(path=SEED_ARTISTS_FILE):
    artists, tracks = {}, {}
    if not path.exists():
        return Profile(source="none")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, w = line.partition("|")
        try:
            weight = max(0.0, min(1.0, float(w))) if w.strip() else 1.0
        except ValueError:
            weight = 1.0
        name = name.strip()
        a, t = split_title(name)
        if a:
            tracks[(normalize(a), normalize(clean_title(t)))] = weight
            key = primary_artist(a)
            artists[key] = max(artists.get(key, (0, a)), (weight, a))
        else:
            artists[normalize(name)] = (weight, name)
    return Profile(artists, tracks, source="seed_artists.txt")


def load_profile():
    """taste_profile.json if built, with seed_artists.txt merged in as overrides."""
    seed = load_seed()
    if not PROFILE_JSON.exists():
        return seed
    data = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
    artists = {normalize(a): (w, a) for a, w in data.get("artists", {}).items()}
    tracks = {}
    for t in data.get("tracks", []):
        key = (normalize(t["artist"]), normalize(clean_title(t["title"])))
        tracks[key] = max(tracks.get(key, 0), t["weight"])
    # seed entries win when they're higher (a way to boost what the export under-counts)
    for k, v in seed.artists.items():
        if v[0] > artists.get(k, (0, None))[0]:
            artists[k] = v
    for k, w in seed.tracks.items():
        tracks[k] = max(tracks.get(k, 0), w)
    source = "taste_profile.json" + (" + seed_artists.txt" if len(seed) else "")
    return Profile(artists, tracks, source=source)

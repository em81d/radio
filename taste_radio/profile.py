"""Taste profile: artist and track weights.

For now this reads seed_artists.txt. In step 3, profile_builder.py will write
taste_profile.json and load_profile() will prefer it; the interface stays the same.
"""
import json
import re

from .config import ROOT, SEED_ARTISTS_FILE
from .util import normalize, primary_artist, split_title

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
        t = normalize(title)
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
            tracks[(normalize(a), normalize(t))] = weight
            key = primary_artist(a)
            artists[key] = max(artists.get(key, (0, a)), (weight, a))
        else:
            artists[normalize(name)] = (weight, name)
    return Profile(artists, tracks, source="seed_artists.txt")


def load_profile():
    if PROFILE_JSON.exists():
        data = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
        artists = {normalize(a): (w, a) for a, w in data.get("artists", {}).items()}
        tracks = {}
        for key, w in data.get("tracks", {}).items():
            a, t = split_title(key)
            if a:
                tracks[(normalize(a), normalize(t))] = w
        return Profile(artists, tracks, source="taste_profile.json")
    return load_seed()

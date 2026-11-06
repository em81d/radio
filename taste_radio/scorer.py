"""Scores a station's current song against the taste profile."""
import time

from . import db
from .explicit import ALLOWED, PENDING
from .nowplaying import is_music
from .util import normalize, primary_artist, split_title

FRESH_SECONDS = 30
FRESH_BONUS = 0.15
REPEAT_HOURS = 3
SONG_REPEAT_PENALTY = 1.0     # same song again: effectively never
ARTIST_REPEAT_PENALTY = 0.25  # per recent play of the same artist


class Scorer:
    def __init__(self, profile, explicit):
        self.profile = profile
        self.explicit = explicit
        self._recent = (0.0, set(), {})

    def _recent_plays(self):
        """(songs, artist counts) played in the last few hours, cached for 30 s."""
        ts, songs, artists = self._recent
        if time.time() - ts > 30:
            songs, artists = set(), {}
            for a, t in db.recent_plays(REPEAT_HOURS):
                songs.add((primary_artist(a), normalize(t)))
                artists[primary_artist(a)] = artists.get(primary_artist(a), 0) + 1
            self._recent = (time.time(), songs, artists)
        return songs, artists

    def taste(self, artist, title):
        """(base score, reason) from the profile alone, ignoring explicit/freshness."""
        bonus = self.profile.track_bonus(artist, title)
        if bonus:
            return 1.0 * bonus, "a song you love"
        weight, name = self.profile.artist_weight(artist)
        if weight:
            return 0.8 * weight, f"artist you like: {name}"
        # Step 4 adds: 0.5 × similarity (similar artists) + 0.3 × tag overlap
        return 0.0, "no profile match"

    def score(self, station, state, now=None):
        """(score, reason, explicit verdict) for one station's current song."""
        now = now or time.time()
        title = state.get("title")
        if not is_music(title, station["name"]):
            return 0.0, "not music", None
        verdict, why = self.explicit.verdict(station, title)
        if verdict != ALLOWED:
            return 0.0, ("waiting for explicit check" if verdict == PENDING
                         else f"explicit ({why})"), verdict

        artist, song = split_title(title)
        s, reason = self.taste(artist, song)
        if s <= 0:
            return 0.0, reason, verdict

        songs, artists = self._recent_plays()
        pa = primary_artist(artist)
        if (pa, normalize(song)) in songs:
            s -= SONG_REPEAT_PENALTY
            reason += ", played recently"
        elif artists.get(pa):
            s -= ARTIST_REPEAT_PENALTY * artists[pa]
            reason += f", artist played {artists[pa]}× recently"

        started = state.get("changed_at")
        if started and now - started < FRESH_SECONDS:
            s += FRESH_BONUS
            reason += ", just started"
        return max(0.0, s), reason, verdict

"""Taste Radio main loop (console).

Run: .\\.venv\\Scripts\\python.exe -m taste_radio.radio
Keys: s = skip (switch now), l = like, q = quit
"""
import os
import sys
import time

from . import db
from .config import CONFIG
from .explicit import ALLOWED, BLOCKED, ExplicitChecker
from .lastfm import TagLookup
from .nowplaying import Poller, is_music
from .player import Player
from .profile import load_profile
from .scorer import Scorer
from .stations import load_pool
from .util import split_title

try:
    import msvcrt
except ImportError:          # not Windows: keys disabled
    msvcrt = None

TICK = 1.0
REDRAW_EVERY = 2.0
EXPLICIT_LOOKAHEAD = 5       # top candidates to explicit-check each tick
HOME_LOOKAHEAD = 2           # best fallback stations to keep explicit-checked
HOME_MARGIN = 0.05           # home must beat the current station's average by this much
STARTUP_WAIT = 20            # seconds to wait for a matching song before going home


class Radio:
    def __init__(self):
        self.profile = load_profile()
        self.pool = load_pool()
        self.by_url = {s["url"]: s for s in self.pool}
        self.order = {s["url"]: i for i, s in enumerate(self.pool)}
        self.explicit = ExplicitChecker()
        self.tags = TagLookup() if self.profile.genres and CONFIG.get("lastfm_api_key") else None
        self.scorer = Scorer(self.profile, self.explicit, self.tags)
        self.player = Player(volume=CONFIG["volume"])
        self.poller = Poller(self.pool, CONFIG["poll_interval"], CONFIG["current_poll_interval"],
                             on_change=self._song_changed)
        self.avg = {}                 # url -> (mean score, n): picks the "home" station
        self.current = None
        self.cur_title = None
        self.cur_reason = ""
        self.tuned_at = 0.0
        self.scores = {}
        self.message = ""
        self.running = True
        self.started_at = time.time()

    def _song_changed(self, url, title):
        """Poller callback (worker thread): learn how often each station fits your taste."""
        station = self.by_url.get(url)
        if not station or not is_music(title, station["name"]):
            return
        artist, song = split_title(title)
        try:
            score, _ = self.scorer.taste(artist, song)
            db.record_station_song(url, station["name"], score)
        except Exception:  # noqa: BLE001 - stats are best-effort
            pass

    # ---- choosing stations -------------------------------------------------
    def home(self, exclude=None):
        """Best station on average whose current song is playable."""
        ok = [u for u, (_, _, v) in self.scores.items()
              if u != exclude and v == ALLOWED]
        if not ok:
            # nothing confirmed clean: settle for a station on an ad/ID (not a song)
            ok = [u for u, (_, r, v) in self.scores.items()
                  if u != exclude and v is None and self.poller_ok(u)]
        if not ok:
            return None
        return max(ok, key=lambda u: (self.avg.get(u, (0, 0))[0], -self.order[u]))

    def best(self, exclude=None):
        cands = [(s, u) for u, (s, _, v) in self.scores.items()
                 if u != exclude and v == ALLOWED and s > 0]
        if cands:
            s, u = max(cands, key=lambda x: (x[0], -self.order[x[1]]))
            if s >= CONFIG["score_threshold"]:
                return u
        return None

    def _mean(self, url):
        return self.avg.get(url, (0.0, 0))[0]

    def poller_ok(self, url):
        st = self.snap.get(url, {})
        return st.get("title") is not None and st.get("error") is None

    def tune(self, url, why):
        if url is None:
            self.player.stop()
            self.current, self.cur_title = None, None
            self.message = f"Nothing safe to play right now ({why}); waiting..."
            return
        if url != self.current:
            self.player.play(url)
            self.poller.set_current(url)
            self.poller.poll_now(url)
            self.current = url
            self.tuned_at = time.time()
        self.message = why
        self.cur_title = self.snap.get(url, {}).get("title")
        self._log_play()

    def _log_play(self):
        if not self.current or not is_music(self.cur_title, self.by_url[self.current]["name"]):
            return
        a, t = split_title(self.cur_title)
        s, reason, _ = self.scores.get(self.current, (0, "", None))
        self.cur_reason = reason
        db.log_play(self.by_url[self.current]["name"], a, t, s, reason)

    # ---- main loop -----------------------------------------------------------
    def tick(self):
        now = time.time()
        self.snap = self.poller.snapshot()

        # Explicit checks: current song first, then the top candidates by taste alone
        cur_state = self.snap.get(self.current, {})
        if self.current and cur_state.get("title"):
            self.explicit.request_check(cur_state["title"], priority=0)
        ranked = []
        for url, st in self.snap.items():
            t = st.get("title")
            if url != self.current and is_music(t, self.by_url[url]["name"]):
                base, _ = self.scorer.taste(*split_title(t))
                if base > 0:
                    ranked.append((base, url, t))
        for _, url, t in sorted(ranked, reverse=True)[:EXPLICIT_LOOKAHEAD]:
            if not self.by_url[url]["plays_clean"]:
                self.explicit.request_check(t, priority=1)
        # Keep a safe fallback ready: check the best "home" stations too
        homes = sorted((u for u, st in self.snap.items()
                        if u != self.current and is_music(st.get("title"), self.by_url[u]["name"])),
                       key=lambda u: (-self.avg.get(u, (0, 0))[0], self.order[u]))
        for u in homes[:HOME_LOOKAHEAD]:
            self.explicit.request_check(self.snap[u]["title"], priority=0 if not self.current else 1)

        self.scores = {url: self.scorer.score(self.by_url[url], st, now)
                       for url, st in self.snap.items() if st.get("title")}
        for url, (s, reason, v) in self.scores.items():
            if reason != "not music":
                m, n = self.avg.get(url, (0.0, 0))
                self.avg[url] = ((m * n + s) / (n + 1), min(n + 1, 200))

        if self.current is None:
            # Give the top candidates' explicit checks a few seconds before settling for home
            target = self.best()
            if target:
                self.tune(target, f"started on a match ({self.scores[target][1]})")
            elif now - self.started_at > STARTUP_WAIT and (target := self.home()):
                self.tune(target, "tuned to the home station")
            else:
                self.message = "Checking songs for explicit lyrics before playing..."
            return

        # Player died: move on
        if self.player.failed():
            self.tune(self.best(self.current) or self.home(self.current),
                      "stream failed; switched")
            return

        new_title = cur_state.get("title")
        verdict = self.scores.get(self.current, (0, "", None))[2]

        # Explicit exception: leave immediately, ignoring the minimum time
        if verdict == BLOCKED:
            self.tune(self.best(self.current) or self.home(self.current),
                      f"left: explicit song ({new_title})")
            return

        if new_title and new_title != self.cur_title:
            self.cur_title = new_title
            if now - self.tuned_at >= CONFIG["min_seconds_per_station"]:
                target = self.best()
                if not target:
                    # nothing good on air: go home only if home is clearly better here
                    h = self.home()
                    if h and self._mean(h) > self._mean(self.current) + HOME_MARGIN:
                        target = h
                if target and target != self.current:
                    s, reason, _ = self.scores[target]
                    self.tune(target, f"song ended; switched ({reason})")
                    return
            self._log_play()

    def keys(self):
        if not msvcrt:
            return
        while msvcrt.kbhit():
            k = msvcrt.getwch().lower()
            if k == "q":
                self.running = False
            elif k == "s" and self.current:
                a, t = split_title(self.cur_title)
                db.log_play(self.by_url[self.current]["name"], a, t, 0, "skipped", event="skip")
                self.tune(self.best(self.current) or self.home(self.current), "skipped")
            elif k == "l" and self.current and self.cur_title:
                a, t = split_title(self.cur_title)
                db.log_play(self.by_url[self.current]["name"], a, t, 0, "liked", event="like")
                self.message = f"Liked: {self.cur_title}"

    def draw(self):
        out = ["\x1b[2J\x1b[H", "TASTE RADIO", "=" * 60]
        if self.current:
            st = self.by_url[self.current]
            s, reason, v = self.scores.get(self.current, (0, "", None))
            out += [f"Station : {st['name']}  [{self.player.state()}]",
                    f"Playing : {self.cur_title or '...'}",
                    f"Why     : {self.cur_reason or reason}",
                    f"Explicit: {v or 'n/a'}"]
        else:
            out.append("Not playing.")
        out += ["", self.message, "", "Next up:"]
        ranked = sorted(((s, u, r) for u, (s, r, v) in self.scores.items()
                         if u != self.current and v == ALLOWED and s > 0), reverse=True)[:3]
        for s, u, r in ranked:
            out.append(f"  {s:4.2f}  {self.snap[u]['title'][:45]:45}  {self.by_url[u]['name'][:22]}  ({r})")
        if not ranked:
            out.append("  (no station is playing a profile match right now)")
        live = sum(1 for v in self.snap.values() if v["title"])
        out += ["", f"Profile: {len(self.profile)} artists from {self.profile.source}   "
                    f"Stations: {live}/{len(self.pool)} reporting   "
                    f"Explicit queue: {self.explicit._q.qsize()}",
                "Keys: [s] skip  [l] like  [q] quit"]
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()

    def run(self):
        self.poller.start()
        self.snap = {}
        last_draw = 0.0
        try:
            while self.running:
                self.tick()
                self.keys()
                if time.time() - last_draw >= REDRAW_EVERY:
                    self.draw()
                    last_draw = time.time()
                time.sleep(TICK)
        except KeyboardInterrupt:
            pass
        finally:
            self.player.stop()
            self.poller.stop()
            print("\nBye.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.system("")            # enable ANSI escape codes in the Windows console
    radio = Radio()
    if not len(radio.profile):
        print("Note: seed_artists.txt has no artists yet, so the radio will stay on the "
              "home station.\n")
        time.sleep(2)
    radio.run()


if __name__ == "__main__":
    main()

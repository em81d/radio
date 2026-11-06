"""Read the current 'Artist - Title' from radio streams (ICY metadata).

Run `python -m taste_radio.nowplaying --test` to print what ~10 stations are playing.
"""
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

from .util import normalize, split_title

UA = "TasteRadio/0.1"
_STREAMTITLE = re.compile(rb"StreamTitle='(.*?)';", re.S)


class NoMetadata(Exception):
    """The stream doesn't send ICY song info."""


def _decode(raw):
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace").strip()


def _read_exact(read, n):
    buf = b""
    while len(buf) < n:
        chunk = read(n - len(buf))
        if not chunk:
            raise ConnectionError("stream ended early")
        buf += chunk
    return buf


def _parse_block(read, metaint):
    _read_exact(read, metaint)                    # skip audio
    length = _read_exact(read, 1)[0] * 16
    if length == 0:
        return ""                                 # no title change in this block
    meta = _read_exact(read, length).rstrip(b"\0")
    m = _STREAMTITLE.search(meta)
    return _decode(m.group(1)) if m else ""


def _fetch_requests(url, timeout):
    headers = {"Icy-MetaData": "1", "User-Agent": UA}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        metaint = r.headers.get("icy-metaint")
        if not metaint:
            raise NoMetadata(url)
        return _parse_block(lambda n: r.raw.read(n, decode_content=False), int(metaint))


def _fetch_socket(url, timeout, redirects=3):
    """Fallback for old SHOUTcast servers that answer 'ICY 200 OK' (not HTTP)."""
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    try:
        if u.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        sock.sendall(
            f"GET {path} HTTP/1.0\r\nHost: {u.hostname}\r\nIcy-MetaData: 1\r\n"
            f"User-Agent: {UA}\r\nConnection: close\r\n\r\n".encode()
        )
        f = sock.makefile("rb")
        status = f.readline().decode("latin-1")
        headers = {}
        while True:
            line = f.readline().decode("latin-1").strip()
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        code = status.split()[1] if len(status.split()) > 1 else ""
        if code.startswith("3") and "location" in headers and redirects:
            return _fetch_socket(headers["location"], timeout, redirects - 1)
        if not code.startswith("2"):
            raise ConnectionError(f"HTTP {status.strip()}")
        if "icy-metaint" not in headers:
            raise NoMetadata(url)
        return _parse_block(f.read, int(headers["icy-metaint"]))
    finally:
        sock.close()


def fetch_title(url, timeout=8):
    """Return the stream's current StreamTitle ('' if blank). Raises on failure."""
    try:
        return _fetch_requests(url, timeout)
    except NoMetadata:
        raise
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
        # BadStatusLine from 'ICY 200 OK' servers shows up as a ConnectionError
        return _fetch_socket(url, timeout)


_NOT_MUSIC = re.compile(
    r"\b(advert|advertisement|advertising|commercial|commercials|jingle|sponsor|"
    r"sponsored|promo|station id|ad break|adbreak|news update|traffic and weather)\b",
    re.I,
)
_NOT_ARTISTS = {"news", "weather", "traffic", "ads", "ad", "advertising", "unknown",
                "unknown artist", "various", "various artists", "radio", "live"}


def is_music(title, station_name=""):
    """True if the title looks like a real 'Artist - Title' song."""
    if not title or _NOT_MUSIC.search(title):
        return False
    artist, song = split_title(title)
    if not artist:
        return False
    a = normalize(artist)
    st = normalize(station_name)
    if not a or a in _NOT_ARTISTS:
        return False
    if st and (a == st or normalize(title) == st):
        return False
    return True


class StationState:
    __slots__ = ("title", "changed_at", "last_poll", "last_ok", "error", "inflight")

    def __init__(self):
        self.title = None
        self.changed_at = None    # when the title last changed; None = unknown
        self.last_poll = 0.0
        self.last_ok = None
        self.error = None
        self.inflight = False


class Poller:
    """Polls many stations in parallel and tracks when each title changes."""

    def __init__(self, stations, interval=25, fast_interval=5, workers=16, on_change=None):
        self.stations = {s["url"]: s for s in stations}
        self.state = {url: StationState() for url in self.stations}
        self.interval = interval
        self.fast_interval = fast_interval
        self.current = None
        self.on_change = on_change
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def set_current(self, url):
        self.current = url

    def poll_now(self, url):
        """Ask for an immediate poll of one station (e.g. right after switching)."""
        with self._lock:
            self.state[url].last_poll = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def snapshot(self):
        with self._lock:
            return {
                url: {"title": st.title, "changed_at": st.changed_at,
                      "last_ok": st.last_ok, "error": st.error}
                for url, st in self.state.items()
            }

    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                for url, st in self.state.items():
                    every = self.fast_interval if url == self.current else self.interval
                    if not st.inflight and now - st.last_poll >= every:
                        st.inflight = True
                        st.last_poll = now
                        self._pool.submit(self._poll, url)
            self._stop.wait(1.0)

    def _poll(self, url):
        try:
            title = fetch_title(url)
            err = None
        except Exception as e:  # noqa: BLE001 - any failure just marks the station down
            title, err = None, f"{type(e).__name__}: {e}"[:120]
        changed = False
        with self._lock:
            st = self.state[url]
            st.inflight = False
            st.error = err
            if err is None:
                st.last_ok = time.time()
                # Blank metadata blocks mean "no change" on many servers; keep old title
                if title and title != st.title:
                    old = st.title
                    st.title = title
                    # first sighting: we don't know when the song started
                    st.changed_at = time.time() if old is not None else None
                    changed = old is not None
        if changed and self.on_change:
            self.on_change(url, title)


def _test():
    from .stations import load_from_file

    stations = [s for s in load_from_file() if s["part"] == 1][:10]
    print(f"Polling {len(stations)} stations once...\n")

    def one(s):
        try:
            return s, fetch_title(s["url"]), None
        except Exception as e:  # noqa: BLE001
            return s, None, f"{type(e).__name__}: {e}"[:80]

    with ThreadPoolExecutor(max_workers=10) as ex:
        for s, title, err in ex.map(one, stations):
            if err:
                print(f"  {s['name'][:34]:34} | ERROR {err}")
            else:
                tag = "" if is_music(title, s["name"]) else "  [not music]"
                print(f"  {s['name'][:34]:34} | {title or '(blank)'}{tag}")


if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        _test()
    else:
        print(__doc__)

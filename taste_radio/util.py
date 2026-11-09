"""Text helpers shared by the scorer and the explicit filter."""
import re
import unicodedata

_FEAT = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring)\s.*$", re.I)
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_NONWORD = re.compile(r"[^\w\s*]")
_SPACES = re.compile(r"\s+")


def normalize(s):
    """Lowercase, drop accents, feat. parts, bracketed suffixes and punctuation.

    '*' is kept so it can act as a wildcard (iTunes masks swear words).
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("&", " and ")
    s = _FEAT.sub("", s)
    s = _BRACKETS.sub("", s)
    s = _NONWORD.sub(" ", s)
    s = s.replace("_", " ")
    return _SPACES.sub(" ", s).strip()


_SUFFIX = re.compile(
    r"\s+[-–—]\s+[^-–—]*\b(remaster(ed)?|live|radio edit|edit|mono|stereo|version|"
    r"mix|remix|from|feat\.?|ft\.?|acoustic|demo|bonus|single|recorded at|session|"
    r"deluxe|instrumental|soundtrack|score)\b[^-–—]*$", re.I)


def clean_title(title):
    """Drop Spotify-style suffixes: 'Somebody - 2006 Remaster' -> 'Somebody'."""
    t = (title or "").strip()
    while True:
        new = _SUFFIX.sub("", t).strip()
        if new == t or not new:
            return t
        t = new


_BY_FORMAT = re.compile(r'^\s*"(?P<title>[^"]+)"\s+by\s+(?P<artist>.+?)(?:\s+from\s+".*")?\s*$', re.I)


def split_title(stream_title):
    """'Artist - Title' -> ('Artist', 'Title'); returns (None, None) if no separator.

    Also understands '"Title" by Artist from "Album"', which some stations send.
    """
    if not stream_title:
        return None, None
    m = _BY_FORMAT.match(stream_title)
    if m:
        return m.group("artist").strip(), m.group("title").strip()
    for sep in (" - ", " – ", " — ", " -- "):
        if sep in stream_title:
            artist, title = stream_title.split(sep, 1)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return artist, title
    return None, None


def primary_artist(artist):
    """'A & B', 'A feat. B', 'A, B' -> 'a' (normalized first credited artist)."""
    a = _FEAT.sub("", artist or "")
    a = re.split(r"\s*(?:,|&|\band\b|\bx\b|/|\bwith\b|\bvs\.?)\s*", a, flags=re.I)[0]
    return normalize(a)


def wildcard_match(pattern, text):
    """Normalized equality where '*' in either string matches any run of characters."""
    if pattern == text:
        return True
    if "*" not in pattern and "*" not in text:
        return False
    for p, t in ((pattern, text), (text, pattern)):
        if "*" in p:
            rx = "^" + ".*".join(re.escape(part) for part in p.split("*")) + "$"
            if re.match(rx, t):
                return True
    return False

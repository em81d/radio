"""Convert a Spotify "Download your data" export into CSV files.

Usage: python export_to_csv.py [export folder]   (default: raw_export)

Writes:
  playlists/<name>.csv          one per playlist
  Liked Songs.csv
  library/*.csv                 saved albums, artists, shows, episodes
  streaming_history/*.csv       music / podcast / audiobook plays
  profile/*.json                everything else, pretty-printed
"""
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote_plus

ROOT = Path(__file__).resolve().parent
ENC = "utf-8-sig"          # Excel needs the BOM to show accents and emoji
BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
HANDLED = set()


def load(path):
    HANDLED.add(path.name)
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding=ENC) as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def safe_name(name, used):
    base = BAD_CHARS.sub("", name).strip().rstrip(".") or "Untitled"
    base = base[:120]
    name, n = base, 2
    while name.lower() in used:
        name = f"{base} ({n})"
        n += 1
    used.add(name.lower())
    return name


def parse_local(uri):
    """spotify:local:Artist:Album:Title:Seconds -> (artist, album, title)."""
    parts = uri.split(":")
    if len(parts) >= 6:
        return tuple(unquote_plus(p) for p in parts[2:5])
    return "", "", unquote_plus(uri)


def item_row(item):
    """(Track, Artist, Album, URI, Type) for any playlist item, or None."""
    if item.get("track"):
        t = item["track"]
        return (t.get("trackName", ""), t.get("artistName", ""), t.get("albumName", ""),
                t.get("trackUri", ""), "track")
    if item.get("localTrack"):
        uri = item["localTrack"].get("uri", "")
        artist, album, title = parse_local(uri)
        return title, artist, album, uri, "localTrack"
    if item.get("episode"):
        e = item["episode"]
        return (e.get("episodeName", ""), e.get("showName", ""), "",
                e.get("episodeUri", ""), "episode")
    for k, v in item.items():
        if k != "addedDate" and v:
            print(f"  warning: unknown playlist item type '{k}' skipped")
            break
    return None


def export_playlists(src, out):
    used, total_pl, total_items = set(), 0, 0
    print("Playlists:")
    for path in sorted(src.glob("Playlist*.json"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
        for pl in load(path).get("playlists", []):
            rows = []
            for i, item in enumerate(pl.get("items", []), 1):
                r = item_row(item)
                if r:
                    track, artist, album, uri, typ = r
                    rows.append((i, track, artist, album, item.get("addedDate", ""), uri, typ))
            name = safe_name(pl.get("name", "Untitled"), used)
            write_csv(out / "playlists" / f"{name}.csv",
                      ["Position", "Track", "Artist", "Album", "Date Added", "Spotify URI", "Type"],
                      rows)
            total_pl += 1
            total_items += len(rows)
            print(f"  {len(rows):6}  {pl.get('name', '')}")
    print(f"  ------\n  {total_items:6}  items in {total_pl} playlists\n")


def export_library(src, out):
    path = src / "YourLibrary.json"
    if not path.exists():
        return
    lib = load(path)
    tracks = lib.get("tracks", [])
    write_csv(out / "Liked Songs.csv", ["Track", "Artist", "Album", "Spotify URI"],
              [(t.get("track", ""), t.get("artist", ""), t.get("album", ""), t.get("uri", ""))
               for t in tracks])
    print(f"Liked Songs: {len(tracks)} (the export has no saved dates)")
    specs = {
        "albums": ["artist", "album", "uri"],
        "artists": ["name", "uri"],
        "shows": ["name", "publisher", "uri"],
        "episodes": ["name", "show", "uri"],
    }
    for key, cols in specs.items():
        items = lib.get(key, [])
        if items:
            write_csv(out / "library" / f"{key}.csv", [c.title() for c in cols],
                      [[x.get(c, "") for c in cols] for x in items])
            print(f"Saved {key}: {len(items)}")
    if lib.get("other"):
        write_csv(out / "library" / "other.csv", ["Uri", "Artist", "Album", "Title"],
                  [(u, *parse_local(u)) if u.startswith("spotify:local:") else (u, "", "", "")
                   for u in lib["other"]])


def export_history(src, out):
    for path in sorted(src.glob("StreamingHistory*.json")):
        rows = load(path)
        if not rows:
            continue
        cols = list(rows[0].keys())
        write_csv(out / "streaming_history" / f"{path.stem}.csv", cols,
                  [[r.get(c, "") for c in cols] for r in rows])
        print(f"{path.stem}: {len(rows)} plays")


def export_rest(src, out):
    for path in sorted(src.glob("*.json")):
        if path.name in HANDLED:
            continue
        dest = out / "profile" / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            dest.write_bytes(path.read_bytes())
    for path in src.glob("*.pdf"):
        (out / "profile" / path.name).write_bytes(path.read_bytes())
    print(f"Other files -> profile/")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "raw_export"
    if not src.is_dir():
        sys.exit(f"Export folder not found: {src}")
    export_playlists(src, ROOT)
    export_library(src, ROOT)
    export_history(src, ROOT)
    export_rest(src, ROOT)


if __name__ == "__main__":
    main()

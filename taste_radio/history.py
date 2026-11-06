"""Show the play log or the explicit-lyrics cache.

python -m taste_radio.history            last 30 plays, likes and skips
python -m taste_radio.history --all      every row
python -m taste_radio.history --cache    explicit-lyrics verdicts
"""
import sys
import time

from . import db


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = db.conn()
    if "--cache" in sys.argv:
        rows = c.execute("SELECT verdict, key, reason FROM explicit_cache "
                         "ORDER BY checked_at DESC").fetchall()
        for verdict, key, reason in rows:
            print(f"{verdict:8}  {key.replace('|', ' - '):55}  {reason}")
        print(f"\n{len(rows)} cached verdicts")
        return
    limit = "" if "--all" in sys.argv else "LIMIT 30"
    rows = c.execute("SELECT ts, event, station, artist, title, reason FROM plays "
                     f"ORDER BY ts DESC {limit}").fetchall()
    for ts, event, station, artist, title, reason in reversed(rows):
        when = time.strftime("%m-%d %H:%M", time.localtime(ts))
        print(f"{when}  {event:4}  {artist} - {title}  [{station}]  ({reason})")
    if not rows:
        print("No plays logged yet.")


if __name__ == "__main__":
    main()

"""Enrich the taste profile with Last.fm genres and similar artists.

python -m taste_radio.enrich            build taste_enrichment.json (top 300 artists)
python -m taste_radio.enrich --top 500  use more artists

First run makes ~600 Last.fm calls (~3 minutes); later runs use the 30-day cache.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .lastfm import LastfmError, similar, top_tags
from .profile import ENRICHMENT_JSON, PROFILE_JSON
from .util import normalize

N_GENRES = 40
MIN_SIMILAR = 0.05


def build(top_n=300):
    if not PROFILE_JSON.exists():
        raise SystemExit("taste_profile.json not found: run python -m taste_radio.profile_builder first")
    profile = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))["artists"]
    top = list(profile.items())[:top_n]
    known = {normalize(a) for a in profile}

    done, failed = [0], []

    def fetch(item):
        name, w = item
        try:
            tags, sims = top_tags(name), similar(name)
        except LastfmError as e:
            failed.append((name, str(e)))
            tags, sims = [], []
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == len(top):
            print(f"\r  Last.fm lookups: {done[0]}/{len(top)}", end="", flush=True)
        return name, w, tags, sims

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(fetch, top))
    print()

    genres, sim, artist_tags = {}, {}, {}
    for name, w, tags, sims in results:
        artist_tags[name] = tags
        for tag, count in tags:
            genres[tag] = genres.get(tag, 0) + w * count / 100
        for other, match in sims:
            s = w * match
            key = normalize(other)
            if s >= MIN_SIMILAR and s > sim.get(key, (0,))[0]:
                sim[key] = (round(s, 4), other, name)

    top_genres = sorted(genres.items(), key=lambda x: -x[1])[:N_GENRES]
    peak = top_genres[0][1] if top_genres else 1
    data = {
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
        "artists_used": len(top),
        "genres": {t: round(v / peak, 4) for t, v in top_genres},
        "similar": {v[1]: {"score": v[0], "via": v[2]}
                    for _, v in sorted(sim.items(), key=lambda x: -x[1][0])},
        "artist_tags": artist_tags,
    }
    ENRICHMENT_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data, known, failed


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    n = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 300
    data, known, failed = build(n)
    print(f"\nTop genres (from your top {data['artists_used']} artists):")
    for tag, w in list(data["genres"].items())[:20]:
        print(f"  {w:5.2f}  {tag}")
    new = [(name, v) for name, v in data["similar"].items() if normalize(name) not in known]
    print(f"\nSimilar artists: {len(data['similar'])} total, {len(new)} not in your profile. Top 20 new:")
    for name, v in new[:20]:
        print(f"  {v['score']:5.2f}  {name}  (via {v['via']})")
    if failed:
        print(f"\n{len(failed)} lookups failed, e.g. {failed[0]}")
    print(f"\nSaved {ENRICHMENT_JSON.name}")


if __name__ == "__main__":
    main()

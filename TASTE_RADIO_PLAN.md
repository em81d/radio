# Plan: Taste Radio — free internet radio that follows your taste

## Context
The user is leaving Spotify and wants a free listening setup that still plays music they like. The idea: build a taste profile from their Spotify data export, watch many free internet radio stations at once, and play whichever station's current song best matches that taste. This uses no paid services and no restricted APIs, and it has no terms-of-service concerns. It is a separate project from `SPOTIFY_EXPORT_PLAN.md`, but it reuses that project's output (the CSVs and the raw export).

**Key limitation:** radio is live, so switching stations joins a song partway through. The design handles this by switching only when the current song ends, and by preferring stations whose song just started.

## Tech
- Python 3.14 (already installed). Dependencies: `requests`, `python-vlc`.
- **VLC desktop** must be installed; it does the actual audio playback.
- **SQLite** (stdlib) for caching lookups and logging plays.
- **Last.fm API key** (free) for genre tags and similar artists. **MusicBrainz** (no key) is the fallback.
- Everything goes in a `taste_radio/` folder inside the project directory. Secrets (the Last.fm key) go in `config.toml`, which is never shared.

## Components
| File | Job |
|---|---|
| `profile_builder.py` | Reads the Spotify export and builds weighted artist and track scores, saved as `taste_profile.json` |
| `enrich.py` | Gets genre tags and similar artists for the profile's top artists from Last.fm, cached in SQLite |
| `stations.py` | Picks candidate stations from the Radio Browser API by matching genre tags, then checks they're alive and send song info |
| `nowplaying.py` | Reads the current `Artist - Title` from each station's stream metadata |
| `scorer.py` | Scores each station's current song against the profile |
| `player.py` | Plays a stream through VLC and switches stations |
| `radio.py` | The main loop and console display; later a small tkinter window |

## How each piece works

**Taste profile (`profile_builder.py`)**
- Artist weight combines:
  - total listening time from Extended streaming history (`ms_played`), with skipped plays down-weighted
  - how often the artist appears in playlists and Liked Songs
  - a bonus for recent listening
- Scores are normalized to 0–1. Track-level scores are kept too, so the exact songs you loved get a bonus.
- If the streaming history hasn't arrived, the profile is built from playlists and Liked Songs alone.
- **Until the export arrives**, `seed_artists.txt` lets the user list favorite artists by hand, so the rest can be built and tested now.

**Enrichment (`enrich.py`)**
- For the top ~300 artists: Last.fm `artist.getTopTags` and `artist.getSimilar`.
- Builds two weighted lists:
  - **genres**: tags weighted by how much you like the artists who carry them
  - **similar artists**: artists Last.fm links to your favorites
- Lookups are cached so they run only once per artist, staying within Last.fm's rate limit (roughly 5 requests per second).

**Station discovery (`stations.py`)**
- Queries Radio Browser by your top ~15 genre tags and keeps working stations with a decent bitrate.
- Tests each one, keeping only stations that send usable song titles (`Artist - Title`, not just the station name).
- Keeps a pool of about 50–150 stations and re-checks it daily.
- SomaFM channels are added from SomaFM's own now-playing feed, which is more reliable.

**Reading the current song (`nowplaying.py`)**
- Connects to the stream with the header `Icy-MetaData: 1`, which asks the station to include song info. The station's response says how often that info appears in the audio.
- Reads just enough audio to reach the first song-info block, pulls out the `StreamTitle` field and disconnects.
- Polls all stations about every 20–30 seconds, several at a time in parallel threads.
- Records when each station's title last *changed*, which is an estimate of when its current song started.
- Titles that are empty, just the station name, or contain words like "advert" or "commercial" are treated as not-music.

**Scoring (`scorer.py`)**
```
score = 1.0  exact track you know and liked
        0.8 × artist_weight   artist in your profile
        0.5 × similarity      artist similar to your favorites (enrichment)
        0.3 × tag_overlap     genre match (artist looked up on the fly, cached)
      − repeat penalty        song or artist played in the last N hours
      + freshness bonus       song started < 30 s ago
      = 0                     not-music (ads, talk)
```

**Switching rules (`radio.py`)**
- Switch only when the current song's title changes, which means it ended or an ad started.
- At that moment, tune to the highest-scoring station, preferring fresh songs.
- If the best score is below a threshold, go to the best "home" station (the one with the highest average score over time).
- A minimum time per station prevents rapid flip-flopping.

**Display and feedback**
- The console shows the current song, why it was picked (e.g. "similar to X"), and the next three candidates.
- Keys: `l` like, `s` skip (switch now and penalize this artist), `q` quit.
- Likes and skips adjust the profile weights.
- Every song played is logged to SQLite and can be exported as `discoveries.csv`.

## Build order
1. **Player and scanner.** This doesn't need the export. Use `player.py`, `nowplaying.py` and `stations.py` with a hard-coded genre list to show the current song across about 50 stations.
2. **Scoring with the seed profile.** Add `seed_artists.txt`, a simple artist-match scorer, and the switching loop. This is the first playable version.
3. **Real profile.** Add `profile_builder.py` once the Spotify export arrives, reusing the parsing from `export_to_csv.py`.
4. **Enrichment.** Add Last.fm tags and similar artists, plus tag-based station discovery.
5. **Polish.** Add likes and skips, the play log and `discoveries.csv`, and optionally a tkinter window and tray icon.

## Verification
- **Step 1:** run `python -m taste_radio.nowplaying --test` and check that the current songs it prints match what the station's own website shows for 3–4 stations.
- **Step 2:** put an artist currently playing on a station into `seed_artists.txt` and confirm the radio switches to that station when the current song ends.
- **Step 3:** compare `taste_profile.json`'s top 20 artists against the user's sense of their favorites, or against Spotify Wrapped.
- **End to end:** run for an hour and check the play log:
  - Share of songs by known or similar artists: target > 50%.
  - Number of ads or talk segments heard: target close to 0.
  - Number of switches that landed mid-song.

## Open items
- **VLC:** check whether VLC is installed; if not, install it or use mpv instead.
- **Last.fm:** the user needs to create a free account and API key at last.fm/api.
- **Station coverage:** coverage for niche genres is unknown until Step 1 is running.

# Taste Radio

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install --id VideoLAN.VLC -e
```

## Spotify export → taste profile
```powershell
.\.venv\Scripts\python.exe export_to_csv.py
.\.venv\Scripts\python.exe -m taste_radio.profile_builder
.\.venv\Scripts\python.exe -m taste_radio.profile_builder --top 100
.\.venv\Scripts\python.exe -m taste_radio.enrich
.\.venv\Scripts\python.exe -m taste_radio.enrich --top 500
```

## Run
```powershell
.\.venv\Scripts\python.exe -m taste_radio.radio
```
Keys: `s` skip · `l` like · `q` quit

## Test
```powershell
.\.venv\Scripts\python.exe -m taste_radio.nowplaying --test
.\.venv\Scripts\python.exe -m taste_radio.explicit --test
.\.venv\Scripts\python.exe -m taste_radio.player
.\.venv\Scripts\python.exe -m taste_radio.player <stream url>
```

## Stations
```powershell
.\.venv\Scripts\python.exe -m taste_radio.stations
.\.venv\Scripts\python.exe -m taste_radio.stations --refresh
.\.venv\Scripts\python.exe -m taste_radio.stations --refresh --no-radio-browser
.\.venv\Scripts\python.exe -m taste_radio.stations --stats
```

## Files to edit
| File | Format |
|---|---|
| `seed_artists.txt` | `Artist` · `Artist \| 0.8` · `Artist - Title` (overrides taste_profile.json when higher) |
| `clean_stations.txt` | station name or stream URL per line |
| `dirty_stations.txt` | station name or stream URL per line |
| `config.toml` | see below |

```toml
[lastfm]
api_key = "..."

[explicit]
unknown_explicit = "allow"   # or "block"

[radio]
poll_interval = 25
current_poll_interval = 5
min_seconds_per_station = 90
score_threshold = 0.2
pool_size = 60
volume = 80
```

## Logs and data
```powershell
.\.venv\Scripts\python.exe -m taste_radio.history
.\.venv\Scripts\python.exe -m taste_radio.history --all
.\.venv\Scripts\python.exe -m taste_radio.history --cache
Get-Content taste_radio\explicit.log -Tail 30
```

## Reset
```powershell
Remove-Item taste_radio\pool.json            # rebuild station pool on next run
Remove-Item taste_radio\taste_radio.db       # clear play log and explicit cache
```

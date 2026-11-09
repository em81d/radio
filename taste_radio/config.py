"""Settings: defaults, overridden by config.toml in the project root if present."""
import tomllib
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent

STATIONS_FILE = ROOT / "radio_stations.txt"
SEED_ARTISTS_FILE = ROOT / "seed_artists.txt"
CLEAN_STATIONS_FILE = ROOT / "clean_stations.txt"
DIRTY_STATIONS_FILE = ROOT / "dirty_stations.txt"
DB_FILE = PKG_DIR / "taste_radio.db"
POOL_FILE = PKG_DIR / "pool.json"

DEFAULTS = {
    "unknown_explicit": "allow",   # "allow" or "block"
    "poll_interval": 25,           # seconds between polls of each station
    "current_poll_interval": 5,    # seconds between polls of the station playing
    "min_seconds_per_station": 90,
    "score_threshold": 0.2,
    "pool_size": 60,
    "volume": 80,
}


def _load():
    cfg = dict(DEFAULTS)
    path = ROOT / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        # Accept both a flat file (api_key = ...) and [lastfm]/[explicit]/[radio] sections
        cfg["lastfm_api_key"] = (data.get("lastfm", {}).get("api_key")
                                 or data.get("lastfm_api_key") or data.get("api_key"))
        flat = {k: v for k, v in data.items() if not isinstance(v, dict)}
        for section in (flat, data.get("explicit", {}), data.get("radio", {})):
            for k, v in section.items():
                if k in DEFAULTS:
                    cfg[k] = v
    if cfg["unknown_explicit"] not in ("allow", "block"):
        raise ValueError('config.toml: unknown_explicit must be "allow" or "block"')
    return cfg


CONFIG = _load()


def lastfm_key():
    key = CONFIG.get("lastfm_api_key")
    if not key:
        raise SystemExit('No Last.fm API key: add  api_key = "..."  to config.toml')
    return key

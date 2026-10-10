"""Downloading IMD gridded rainfall and saving it as observation records (unfrozen, KTD9).

Needs the ``obs`` extra: ``imdlib`` does the downloads. Its files land under
``.cache/evidence/observations/<vintage>/``, one directory per vintage so a
later retrieval never reuses an earlier day's bytes. The raw files cover all of
India; only the Mandi box is committed, with each raw file's SHA-256.

- ``final``: IMD's yearly archive files, one record per year.
- ``realtime``: IMD's daily provisional files, one record per run of dates.

Vintages are named after the UTC date of retrieval. A year whose raw file is
already saved under an earlier final vintage is skipped, so fetching again adds
a file only when IMD has changed that year.
"""

import hashlib
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from khetru_evidence import obs_core

LEDGER = "mandi-wheat"
CACHE_DIR = "observations"
FINAL_URL = "https://imdpune.gov.in/cmpg/Griddata/rainfall.php"
REALTIME_URL = "https://imdpune.gov.in/cmpg/Realtimedata/Rainfall/rain.php"


def vintage_name(product: str, retrieved: date) -> str:
    return f"{product}-r{retrieved:%Y%m%d}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _saved_raw(ledger_root: Path, product: str) -> set[tuple[str, str]]:
    """(raw file name, SHA-256) of every raw file behind a saved record of ``product``."""
    saved = set()
    for path in sorted((ledger_root / obs_core.OBSERVATIONS_DIR).glob(f"imd-{product}-*.json.gz")):
        record = obs_core.decode_record(path.read_bytes())
        saved |= {(item["name"], item["sha256"]) for item in record["raw"]}
    return saved


def final_record(raw: bytes, year: int, vintage: str) -> dict:
    days = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    return obs_core.build_record(
        obs_core.decode_grd(raw, days), product="final", vintage=vintage, first=date(year, 1, 1),
        source_url=FINAL_URL, raw=[(f"{year}.grd", _sha256(raw))],
    )


def realtime_record(raw_by_day: dict[date, bytes], vintage: str) -> dict:
    days = sorted(raw_by_day)
    if days != [days[0] + timedelta(days=i) for i in range(len(days))]:
        raise obs_core.ObservationError("real-time days must be consecutive")
    grid = [obs_core.decode_grd(raw_by_day[day], 1)[0] for day in days]
    return obs_core.build_record(
        grid, product="realtime", vintage=vintage, first=days[0], source_url=REALTIME_URL,
        raw=[(f"{day:%Y-%m-%d}.grd", _sha256(raw_by_day[day])) for day in days],
    )


def fetch_final(first_year: int, last_year: int, *, repo: Path, retrieved: date | None = None) -> int:
    """Save each year's final record; 0 when every year is saved, 1 when a download failed."""
    import imdlib

    retrieved = retrieved or datetime.now(UTC).date()
    vintage = vintage_name("final", retrieved)
    ledger_root = repo / "ledger" / LEDGER
    cache = repo / ".cache" / "evidence" / CACHE_DIR / vintage
    saved = _saved_raw(ledger_root, "final")
    failed = 0
    for year in range(first_year, last_year + 1):
        path = cache / "rain" / f"{year}.grd"
        try:
            imdlib.get_data("rain", year, year, fn_format="yearwise", file_dir=str(cache))
            raw = path.read_bytes()
        except Exception as e:  # imdlib raises bare Exception for an unpublished year
            print(f"final {year}: download failed: {e}", file=sys.stderr)
            failed = 1
            continue
        if (path.name, _sha256(raw)) in saved:
            print(f"final {year}: already saved, unchanged at IMD")
            continue
        rel, _ = obs_core.write_record(ledger_root, final_record(raw, year, vintage))
        print(f"saved {rel}")
    return failed


def fetch_realtime(first: date, last: date, *, repo: Path, retrieved: date | None = None) -> int:
    """Save one real-time record for ``first``..``last``; 1 when any day could not be downloaded."""
    import imdlib

    retrieved = retrieved or datetime.now(UTC).date()
    vintage = vintage_name("realtime", retrieved)
    cache = repo / ".cache" / "evidence" / CACHE_DIR / vintage
    cache.mkdir(parents=True, exist_ok=True)
    raw_by_day = {}
    day = first
    while day <= last:
        path = cache / f"rain_ind0.25_{day:%y_%m_%d}.grd"
        try:
            imdlib.get_real_data("rain", day.isoformat(), day.isoformat(), file_dir=str(cache))
            raw_by_day[day] = path.read_bytes()
        except Exception as e:
            print(f"realtime {day}: download failed: {e}", file=sys.stderr)
            return 1
        day += timedelta(days=1)
    rel, written = obs_core.write_record(repo / "ledger" / LEDGER, realtime_record(raw_by_day, vintage))
    print(f"saved {rel}" if written else f"{rel} is already saved")
    return 0

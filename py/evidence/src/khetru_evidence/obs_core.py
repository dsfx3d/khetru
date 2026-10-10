"""Observed rainfall: the normalised IMD record, band window totals, rain episodes, climatology.

Pure; frozen at the bundle tag (KTD9). ``fetch_observations`` downloads IMD's
0.25° gridded daily rainfall and hands the raw bytes to ``decode_grd`` and
``build_record``; ``observations`` loads saved records and makes the base-rate
table.

A record holds one product (``final`` or ``realtime``) for a run of consecutive
dates, as one vintage:

- daily rainfall in mm on the IMD lattice cells of the Mandi box plus one cell
  of margin, the same cells as the forecast records; ``null`` where IMD has no
  value;
- the vintage name, ``<product>-r<YYYYMMDD>`` after the UTC date of retrieval;
- the source URL and the SHA-256 of each raw IMD file the values came from.

On disk a record is ``observations/imd-<vintage>-<first>-<last>.json.gz``,
encoded like a forecast record (canonical JSON, gzip level 9, zero mtime).
Files are write-once evidence (KTD6): a later retrieval of the same dates is a
new vintage in a new file, and both are kept.

IMD dates: station rainfall is read at 08:30 IST (03:00 UTC), and the 24 hours
ending then are recorded against that date. A rain-day from ``semantics`` that
ends at 03:00 UTC on date D is therefore IMD's date D.

Values are rounded to 0.001 mm; IMD's files hold 32-bit floats.
"""

import gzip
import json
import math
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from khetru_evidence import bands
from khetru_evidence.forecasts import MANDI_BOX, MARGIN_CELLS, Box, LatticeError, box_cells, write_once
from khetru_evidence.ledger import encode
from khetru_evidence.semantics import Schedule

FORMAT = "khetru-imd-record/1"
PRODUCTS = ("final", "realtime")
OBSERVATIONS_DIR = "observations"
DECIMALS = 3  # mm

# IMD 0.25° rainfall files, archive and real-time: little-endian float32, one
# record per day, 129 latitudes from 6.5°N by 135 longitudes from 66.5°E, south
# to north then west to east (imdpune.gov.in/cmpg/Griddata/Rainfall_25_Bin.html).
GRD_LAT0, GRD_LON0 = 6.5, 66.5
GRD_NLAT, GRD_NLON = 129, 135
GRD_MISSING = -999.0
IMD_DAY_END_HOUR_UTC = 3  # 08:30 IST

HELD, NOT_HELD, UNVERIFIABLE = "held", "not_held", "unverifiable"

_EPS = 1e-6
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VINTAGE = re.compile(r"(final|realtime)-r\d{8}")


class ObservationError(ValueError):
    """An observation record cannot be built, read or used as asked."""


@dataclass(frozen=True)
class Rules:
    """Observation rule values, read from a bundle's ``[observations]`` table."""

    coverage_share: float  # share of a band's weight that must hold a value (KTD4)
    wet_day_mm: float  # a band-mean day at or above this is a wet day
    dry_day_gap_days: int  # wet days fewer than this many dry days apart are one episode
    climatology_first_year: int
    climatology_half_window_days: int  # windows starting within ± this of the date are pooled
    climatology_pseudo_count: float  # added to events and to non-events
    sowing_cutoff: tuple[int, int]  # (month, day), last IMD date of "rain before the cutoff"


@dataclass(frozen=True)
class Daily:
    """One vintage's daily cell values: ``values[i]`` is the grid for ``dates[i]``, NaN where missing."""

    vintage: str
    dates: tuple[date, ...]
    lats: tuple[float, ...]
    lons: tuple[float, ...]
    values: np.ndarray  # shape (len(dates), len(lats), len(lons))


@dataclass(frozen=True)
class Climatology:
    probability: float
    events: int
    windows: int


def load_rules(toml_path: Path) -> Rules:
    with open(toml_path, "rb") as f:
        o = tomllib.load(f)["observations"]
    month, day = o["sowing_cutoff"].split("-")
    rules = Rules(
        coverage_share=float(o["coverage_share"]),
        wet_day_mm=float(o["wet_day_mm"]),
        dry_day_gap_days=int(o["dry_day_gap_days"]),
        climatology_first_year=int(o["climatology_first_year"]),
        climatology_half_window_days=int(o["climatology_half_window_days"]),
        climatology_pseudo_count=float(o["climatology_pseudo_count"]),
        sowing_cutoff=(int(month), int(day)),
    )
    if not 0 < rules.coverage_share <= 1:
        raise ObservationError("coverage_share must be above 0 and at most 1")
    if rules.dry_day_gap_days < 1 or rules.climatology_half_window_days < 0:
        raise ObservationError("dry_day_gap_days must be at least 1 and the half window at least 0")
    if not rules.climatology_pseudo_count > 0:
        raise ObservationError("climatology_pseudo_count must be above 0, or a probability can be 0 or 1")
    return rules


# --- the normalised record ---------------------------------------------------


def decode_grd(raw: bytes, days: int) -> np.ndarray:
    """IMD ``.grd`` bytes as mm per day, shape (days, 129, 135), NaN where IMD has no value."""
    size = days * GRD_NLAT * GRD_NLON * 4
    if len(raw) != size:
        raise ObservationError(f"expected {size} bytes for {days} day(s) of IMD grid, got {len(raw)}")
    grid = np.frombuffer(raw, dtype="<f4").astype(float).reshape(days, GRD_NLAT, GRD_NLON)
    grid[grid == GRD_MISSING] = np.nan
    if not (np.isnan(grid) | (grid >= 0)).all():
        raise ObservationError("IMD grid holds a negative value other than the missing value")
    return grid


def build_record(grid: np.ndarray, *, product: str, vintage: str, first: date, source_url: str,
                 raw: Sequence[tuple[str, str]], box: Box = MANDI_BOX,
                 margin: int = MARGIN_CELLS) -> dict:
    """The normalised record of ``grid`` (days, 129, 135) starting on ``first``.

    ``raw`` lists (file name, SHA-256) of every IMD file the grid was read from.
    """
    grid = np.asarray(grid, dtype=float)
    if grid.ndim != 3 or grid.shape[1:] != (GRD_NLAT, GRD_NLON):
        raise ObservationError(f"grid shape {grid.shape} is not (days, {GRD_NLAT}, {GRD_NLON})")
    lats, lons = box_cells(box, margin)
    rows = [round((lat - GRD_LAT0) / bands.LATTICE) for lat in lats]
    cols = [round((lon - GRD_LON0) / bands.LATTICE) for lon in lons]
    if rows[0] < 0 or rows[-1] >= GRD_NLAT or cols[0] < 0 or cols[-1] >= GRD_NLON:
        raise LatticeError("the box reaches beyond the IMD grid")
    cut = np.round(grid[:, rows][:, :, cols], DECIMALS) + 0.0
    record = {
        "format": FORMAT,
        "product": product,
        "vintage": vintage,
        "first": first.isoformat(),
        "units": "mm",
        "lats": lats,
        "lons": lons,
        "source_url": source_url,
        "raw": [{"name": name, "sha256": sha256} for name, sha256 in raw],
        "values": [[[None if math.isnan(v) else v for v in row] for row in day] for day in cut.tolist()],
    }
    validate_record(record)
    return record


def validate_record(record: dict) -> None:
    """Raise unless ``record`` is a well-formed observation record on the IMD lattice."""
    keys = {"format", "product", "vintage", "first", "units", "lats", "lons", "source_url", "raw", "values"}
    if not isinstance(record, dict) or set(record) != keys:
        raise ObservationError(f"record must have exactly the fields {sorted(keys)}")
    if record["format"] != FORMAT:
        raise ObservationError(f"format must be {FORMAT}")
    if record["product"] not in PRODUCTS:
        raise ObservationError(f"product must be one of {', '.join(PRODUCTS)}")
    vintage = record["vintage"]
    if not isinstance(vintage, str) or not _VINTAGE.fullmatch(vintage) \
            or not vintage.startswith(record["product"] + "-"):
        raise ObservationError("vintage must be the product then -r and the retrieval date, like final-r20261010")
    try:
        date.fromisoformat(record["first"])
    except (TypeError, ValueError):
        raise ObservationError("first must be an ISO date") from None
    if record["units"] != "mm":
        raise ObservationError("units must be mm")
    if not isinstance(record["source_url"], str) or not record["source_url"]:
        raise ObservationError("source_url must be a non-empty string")
    for axis in ("lats", "lons"):
        coords = record[axis]
        if not coords or not all(isinstance(c, (int, float)) for c in coords):
            raise ObservationError(f"{axis} must be a non-empty list of numbers")
        if any(abs(c / bands.LATTICE - round(c / bands.LATTICE)) > _EPS for c in coords):
            raise LatticeError(f"{axis} holds a value off the IMD {bands.LATTICE}° lattice")
        if any(abs(b - a - bands.LATTICE) > _EPS for a, b in zip(coords, coords[1:])):
            raise LatticeError(f"{axis} must be consecutive ascending lattice cells")
    raw = record["raw"]
    if not isinstance(raw, list) or not raw:
        raise ObservationError("raw must list the IMD files the values came from")
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "sha256"} \
                or not isinstance(item["name"], str) or not item["name"] \
                or not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]):
            raise ObservationError("each raw file needs a name and a SHA-256 of 64 lowercase hex digits")
    _grid(record)


def _grid(record: dict) -> np.ndarray:
    shape = (len(record["lats"]), len(record["lons"]))
    values = record["values"]
    try:
        grid = np.array(values, dtype=float)  # None becomes NaN
    except (TypeError, ValueError):
        raise ObservationError("values is not a numeric grid per day") from None
    if not values or grid.ndim != 3 or grid.shape[1:] != shape:
        raise ObservationError(f"values must hold one grid of shape {shape} per day")
    if np.isinf(grid).any() or (grid < 0).any():
        raise ObservationError("values must be null or a rainfall of 0 mm or more")
    return grid


def encode_record(record: dict) -> bytes:
    validate_record(record)
    return gzip.compress(encode(record), compresslevel=9, mtime=0)


def decode_record(data: bytes) -> dict:
    try:
        text = gzip.decompress(data)
        record = json.loads(text)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ObservationError(f"not a gzip JSON record: {e}") from None
    validate_record(record)
    if encode(record) != text:
        raise ObservationError("record JSON is not canonical")
    return record


def record_dates(record: dict) -> tuple[date, ...]:
    first = date.fromisoformat(record["first"])
    return tuple(first + timedelta(days=i) for i in range(len(record["values"])))


def record_path(record: dict) -> str:
    dates = record_dates(record)
    return f"{OBSERVATIONS_DIR}/imd-{record['vintage']}-{dates[0]:%Y%m%d}-{dates[-1]:%Y%m%d}.json.gz"


def write_record(ledger_root: Path, record: dict) -> tuple[str, bool]:
    """Save ``record`` write-once; returns (relative path, whether a file was written)."""
    rel = record_path(record)
    try:
        return rel, write_once(Path(ledger_root) / rel, encode_record(record))
    except FileExistsError:
        raise ObservationError(
            f"{rel} already exists with different content; observations are write-once"
        ) from None


def daily(records: Iterable[dict]) -> Daily:
    """The records of one vintage as one series; they must share a grid and not overlap."""
    records = sorted(records, key=lambda r: r["first"])
    if not records:
        raise ObservationError("no observation records")
    head = records[0]
    dates: list[date] = []
    for record in records:
        validate_record(record)
        if any(record[f] != head[f] for f in ("vintage", "lats", "lons")):
            raise ObservationError("records of one series must share a vintage and a grid")
        dates += record_dates(record)
    if dates != sorted(set(dates)):
        raise ObservationError(f"records of vintage {head['vintage']} overlap in dates")
    values = np.concatenate([_grid(r) for r in records])
    return Daily(head["vintage"], tuple(dates), tuple(head["lats"]), tuple(head["lons"]), values)


# --- band values, windows and outcomes ---------------------------------------


def band_daily(series: Daily, band_map: bands.BandMap, verdict_band: str,
               coverage_share: float) -> dict[date, float]:
    """Band-mean rainfall per date; NaN where the band's coverage is short (KTD4)."""
    mean = bands.band_mean(band_map, verdict_band, series.lats, series.lons, series.values, coverage_share)
    return dict(zip(series.dates, mean.values.tolist()))


def imd_dates(schedule: Schedule) -> tuple[date, ...]:
    """The IMD date of each rain-day of a claim's observed window."""
    for _, end in schedule.rain_days:
        if (end.hour, end.minute, end.utcoffset()) != (IMD_DAY_END_HOUR_UTC, 0, timedelta(0)):
            raise ObservationError(f"IMD days end at {IMD_DAY_END_HOUR_UTC:02d}:00 UTC; a rain-day ends at {end}")
    return tuple(end.date() for _, end in schedule.rain_days)


def window_total(band: Mapping[date, float], dates: Sequence[date]) -> float | None:
    """Band rainfall over ``dates``; None when any of them has no band value."""
    values = [band.get(d, math.nan) for d in dates]
    if not values or any(math.isnan(v) for v in values):
        return None
    return math.fsum(values)


def outcome(total: float | None, threshold_mm: float) -> str:
    if total is None:
        return UNVERIFIABLE
    return HELD if total >= threshold_mm else NOT_HELD


def run_dates(first: date, days: int) -> tuple[date, ...]:
    return tuple(first + timedelta(days=i) for i in range(days))


def rain_before(band: Mapping[date, float], first: date, last: date, days: int,
                threshold_mm: float) -> str:
    """Whether some run of ``days`` dates within ``first``..``last`` reaches the threshold.

    ``unverifiable`` when no run reaches it and some run has a date with no value.
    """
    seen = NOT_HELD
    start = first
    while start + timedelta(days=days - 1) <= last:
        result = outcome(window_total(band, run_dates(start, days)), threshold_mm)
        if result == HELD:
            return HELD
        if result == UNVERIFIABLE:
            seen = UNVERIFIABLE
        start += timedelta(days=1)
    return seen


# --- independent rain episodes (KTD8) ----------------------------------------


def rain_episodes(band: Mapping[date, float], first: date, last: date, wet_day_mm: float,
                  dry_day_gap_days: int) -> list[tuple[date, date]]:
    """Rain episodes within ``first``..``last`` as (first wet day, last wet day).

    A wet day has a band value of ``wet_day_mm`` or more. Wet days with fewer
    than ``dry_day_gap_days`` dry days between them belong to one episode. A
    date with no value counts as dry.
    """
    episodes: list[tuple[date, date]] = []
    day = first
    while day <= last:
        if band.get(day, math.nan) >= wet_day_mm:
            if episodes and (day - episodes[-1][1]).days - 1 < dry_day_gap_days:
                episodes[-1] = (episodes[-1][0], day)
            else:
                episodes.append((day, day))
        day += timedelta(days=1)
    return episodes


def episode_counts(band: Mapping[date, float], windows: Sequence[Sequence[date]], threshold_mm: float,
                   wet_day_mm: float, dry_day_gap_days: int) -> tuple[int, int]:
    """(positive, negative) independent episodes among a season's windows, in time order.

    Held windows that hold wet days of the same rain episode are one positive
    episode. A run of windows in a row that did not hold is one negative
    episode. Unverifiable windows count for neither and do not break a run.
    """
    judged = [(w, outcome(window_total(band, w), threshold_mm)) for w in windows]
    judged = [(w, result) for w, result in judged if result != UNVERIFIABLE]
    if not judged:
        return 0, 0
    episodes = rain_episodes(band, min(w[0] for w, _ in judged), max(w[-1] for w, _ in judged),
                             wet_day_mm, dry_day_gap_days)
    positive = negative = 0
    previous: tuple[str, set[int]] | None = None
    for window, result in judged:
        own = {i for i, (a, b) in enumerate(episodes)
               if any(a <= d <= b and band.get(d, math.nan) >= wet_day_mm for d in window)}
        if result == HELD:
            if not (previous and previous[0] == HELD and previous[1] & own):
                positive += 1
        elif not (previous and previous[0] == NOT_HELD):
            negative += 1
        previous = (result, own)
    return positive, negative


# --- climatology (KTD8) ------------------------------------------------------


def climatology(band: Mapping[date, float], first: date, *, years: Iterable[int], days: int,
                threshold_mm: float, half_window_days: int, pseudo_count: float) -> Climatology:
    """Climatological probability that the ``days``-day window starting on ``first`` holds.

    Pools, over ``years``, every window starting within ``half_window_days`` of
    ``first``'s calendar date. Leave a season out by leaving its year out of
    ``years``. Windows with a date that has no value are skipped. The
    probability is (events + pseudo) / (windows + 2 * pseudo), never 0 or 1.
    """
    if not pseudo_count > 0:
        raise ObservationError("pseudo_count must be above 0, or the probability can be 0 or 1")
    events = windows = 0
    for year in sorted(set(years)):
        centre = first.replace(year=year)
        for shift in range(-half_window_days, half_window_days + 1):
            result = outcome(window_total(band, run_dates(centre + timedelta(days=shift), days)), threshold_mm)
            if result != UNVERIFIABLE:
                windows += 1
                events += result == HELD
    return Climatology((events + pseudo_count) / (windows + 2 * pseudo_count), events, windows)

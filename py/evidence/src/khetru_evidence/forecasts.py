"""Normalised ECMWF ENS forecast record (KTD2). Pure; frozen at the bundle tag (KTD9).

Both adapters (open data and TIGGE, in ``fetch_forecasts``) decode GRIB into
``Field`` values and hand them to ``build_record``, so one record format holds
every run:

- the control and every perturbed member, kept separate;
- cumulative total precipitation in mm at every native step from 0 to 360 h;
- cells on the IMD 0.25° lattice (centres at multiples of 0.25°), cropped to the
  Mandi box plus one cell of margin;
- the init time, model cycle, source URL and SHA-256 of the raw GRIB bytes.

Window totals are never stored; ``window_totals`` rebuilds them for any step
range, so a window chosen later (U8) still works on rabi 2026 data.

On disk a record is ``inputs/ens-<source>-<YYYYMMDDHH>.json.gz``: the canonical
JSON of the record (sorted keys, compact separators, trailing newline, as in the
ledger) compressed with gzip at level 9 and a zero mtime, so the same record
always gives the same bytes. Files are write-once evidence (KTD6).

Values are rounded to 0.001 mm; GRIB packing error is far below that.
"""

import gzip
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from khetru_evidence.ledger import encode

FORMAT = "khetru-ens-record/1"
LATTICE = 0.25  # IMD gridded rainfall: 0.25° cells centred on multiples of 0.25°
MARGIN_CELLS = 1
DECIMALS = 3  # mm
INPUTS_DIR = "inputs"
SOURCES = ("opendata", "tigge")

# Native tp steps (h). Open data: 0-144 by 3, then 150-360 by 6. TIGGE: 0-360 by 6.
OPENDATA_STEPS = (*range(0, 144, 3), *range(144, 361, 6))
TIGGE_STEPS = tuple(range(0, 361, 6))
ENS_PERTURBED = tuple(range(1, 51))

# GRIB units of total precipitation -> factor to mm. Open data tp is in metres of
# water; TIGGE tp (param 228228) is in kg m-2, which equals mm.
MM_PER_UNIT = {"m": 1000.0, "kg m**-2": 1.0}

_EPS = 1e-6
_SHA256 = re.compile(r"[0-9a-f]{64}")
_INIT = "%Y-%m-%dT%H:%M:%SZ"


class RecordError(ValueError):
    """A forecast record cannot be built, read or written as asked."""


class IncompleteRunError(RecordError):
    """A run lacks a step or member; it is a failure, never a shorter series."""


class LatticeError(RecordError):
    """Cell coordinates are off the IMD 0.25° lattice or do not cover the box."""


@dataclass(frozen=True)
class Box:
    """A geographic extent in degrees; its record covers every lattice cell it touches."""

    south: float
    north: float
    west: float
    east: float


# Mandi district extent, 31°13'20"-32°04'30" N, 76°37'20"-77°23'15" E, from the
# KVK Mandi district profile (hillagric.ac.in/.../kvk_mandi/pdf/AboutDistrict.pdf).
# U3's district polygon is the authority; widen this if the polygon pokes out.
MANDI_BOX = Box(
    south=31 + 13 / 60 + 20 / 3600,
    north=32 + 4 / 60 + 30 / 3600,
    west=76 + 37 / 60 + 20 / 3600,
    east=77 + 23 / 60 + 15 / 3600,
)


@dataclass(frozen=True)
class Field:
    """One decoded GRIB message: one member's cumulative tp at one step.

    ``values`` has shape (len(lats), len(lons)) in the order of ``lats``/``lons``.
    ``member`` is "control" or "perturbed"; ``number`` is the ensemble number.
    """

    member: str
    number: int
    init: datetime
    step: int
    units: str
    lats: Sequence[float]
    lons: Sequence[float]
    values: np.ndarray
    process_id: int | None = None  # GRIB generatingProcessIdentifier, if known


# --- lattice and cropping ----------------------------------------------------


def _cell(x: float) -> int:
    """Lattice index of the 0.25° cell containing ``x``."""
    return math.floor(x / LATTICE + 0.5)


def box_cells(box: Box, margin: int = MARGIN_CELLS) -> tuple[list[float], list[float]]:
    """Ascending lattice-cell centres covering ``box`` plus ``margin`` cells each side."""
    lat = range(_cell(box.south) - margin, _cell(box.north) + margin + 1)
    lon = range(_cell(box.west) - margin, _cell(box.east) + margin + 1)
    return [i * LATTICE for i in lat], [i * LATTICE for i in lon]


def _on_lattice(x: float) -> bool:
    return abs(x / LATTICE - round(x / LATTICE)) < _EPS


def _normal_lon(lon: float) -> float:
    return lon - 360.0 if lon > 180.0 else lon


def _select(coords: Sequence[float], wanted: list[float], axis: str) -> list[int]:
    """Indices of ``wanted`` in ``coords``; every coordinate inside the span must be on the lattice."""
    lo, hi = wanted[0] - LATTICE / 2, wanted[-1] + LATTICE / 2
    inside = {i: c for i, c in enumerate(coords) if lo - _EPS <= c <= hi + _EPS}
    off = sorted(c for c in inside.values() if not _on_lattice(c))
    if off:
        raise LatticeError(f"{axis} {off[0]} is off the IMD {LATTICE}° lattice")
    index = {round(c / LATTICE): i for i, c in inside.items()}
    missing = [w for w in wanted if round(w / LATTICE) not in index]
    if missing:
        raise LatticeError(f"grid does not cover {axis} {missing[0]} of the box")
    return [index[round(w / LATTICE)] for w in wanted]


def crop(field: Field, box: Box = MANDI_BOX, margin: int = MARGIN_CELLS) -> Field:
    """``field`` cut to the box plus margin, lats and lons ascending."""
    lats, lons = box_cells(box, margin)
    values = np.asarray(field.values, dtype=float)
    if values.shape != (len(field.lats), len(field.lons)):
        raise RecordError(f"values shape {values.shape} does not match the grid")
    rows = _select([float(x) for x in field.lats], lats, "latitude")
    cols = _select([_normal_lon(float(x)) for x in field.lons], lons, "longitude")
    return Field(
        member=field.member, number=field.number, init=field.init, step=field.step,
        units=field.units, lats=lats, lons=lons, values=values[np.ix_(rows, cols)],
        process_id=field.process_id,
    )


# --- building a record -------------------------------------------------------


def build_record(
    fields: Iterable[Field],
    *,
    source: str,
    init: datetime,
    steps: Sequence[int],
    perturbed: Iterable[int],
    model_cycle: str,
    source_url: str,
    raw_sha256: str,
    box: Box = MANDI_BOX,
    margin: int = MARGIN_CELLS,
) -> dict:
    """The normalised record of one run; refuses a run missing any step or member."""
    steps = sorted(steps)
    wanted = sorted(perturbed)
    lats, lons = box_cells(box, margin)
    grids: dict[tuple[str, int], dict[int, list]] = {}
    for field in fields:
        if field.init != init:
            raise RecordError(f"field init {field.init} is not the run's init {init}")
        if field.step not in steps:
            raise RecordError(f"unexpected step {field.step} h")
        if field.units not in MM_PER_UNIT:
            raise RecordError(f"unknown precipitation units {field.units!r}")
        if field.member == "control":
            key = ("control", 0)
        elif field.member == "perturbed" and field.number in wanted:
            key = ("perturbed", field.number)
        else:
            raise RecordError(f"unexpected member {field.member} {field.number}")
        member_steps = grids.setdefault(key, {})
        if field.step in member_steps:
            raise RecordError(f"duplicate field for {key[0]} {key[1]} at step {field.step} h")
        cut = crop(field, box, margin)
        mm = np.round(cut.values * MM_PER_UNIT[field.units], DECIMALS) + 0.0
        member_steps[field.step] = mm.tolist()

    expected = [("control", 0), *(("perturbed", n) for n in wanted)]
    for key in expected:
        if key not in grids:
            raise IncompleteRunError(f"{source} run {init:%Y-%m-%dT%HZ} has no {key[0]} member {key[1]}")
        missing = [s for s in steps if s not in grids[key]]
        if missing:
            listed = ", ".join(str(s) for s in missing)
            raise IncompleteRunError(
                f"{source} run {init:%Y-%m-%dT%HZ} {key[0]} {key[1]} is missing step(s) {listed} h"
            )

    def series(key):
        return [grids[key][s] for s in steps]

    record = {
        "format": FORMAT,
        "source": source,
        "init": init.astimezone(UTC).strftime(_INIT),
        "model_cycle": model_cycle,
        "source_url": source_url,
        "raw_sha256": raw_sha256,
        "units": "mm",
        "lats": lats,
        "lons": lons,
        "steps": steps,
        "control": series(("control", 0)),
        "perturbed": {str(n): series(("perturbed", n)) for n in wanted},
    }
    validate_record(record)
    return record


# --- reading and checking ----------------------------------------------------


def validate_record(record: dict) -> None:
    """Raise unless ``record`` is a well-formed normalised record on the IMD lattice."""
    keys = {"format", "source", "init", "model_cycle", "source_url", "raw_sha256", "units",
            "lats", "lons", "steps", "control", "perturbed"}
    if not isinstance(record, dict) or set(record) != keys:
        raise RecordError(f"record must have exactly the fields {sorted(keys)}")
    for axis in ("lats", "lons"):
        coords = record[axis]
        if not coords or not all(isinstance(c, (int, float)) for c in coords):
            raise RecordError(f"{axis} must be a non-empty list of numbers")
        off = [c for c in coords if not _on_lattice(c)]
        if off:
            raise LatticeError(f"{axis} value {off[0]} is off the IMD {LATTICE}° lattice")
        if any(abs(b - a - LATTICE) > _EPS for a, b in zip(coords, coords[1:])):
            raise LatticeError(f"{axis} must be consecutive ascending lattice cells")
    if record["format"] != FORMAT:
        raise RecordError(f"format must be {FORMAT}")
    if record["source"] not in SOURCES:
        raise RecordError(f"source must be one of {', '.join(SOURCES)}")
    try:
        datetime.strptime(record["init"], _INIT)
    except (TypeError, ValueError):
        raise RecordError("init must be UTC like 2026-10-08T00:00:00Z") from None
    if record["units"] != "mm":
        raise RecordError("units must be mm")
    if not isinstance(record["raw_sha256"], str) or not _SHA256.fullmatch(record["raw_sha256"]):
        raise RecordError("raw_sha256 must be 64 lowercase hex digits")
    for f in ("model_cycle", "source_url"):
        if not isinstance(record[f], str) or not record[f]:
            raise RecordError(f"{f} must be a non-empty string")
    steps = record["steps"]
    if not steps or steps != sorted(set(steps)) or not all(type(s) is int for s in steps):
        raise RecordError("steps must be ascending distinct integers")
    shape = (len(steps), len(record["lats"]), len(record["lons"]))
    perturbed = record["perturbed"]
    if not isinstance(perturbed, dict) or not all(k.isdigit() for k in perturbed):
        raise RecordError("perturbed must map member numbers to series")
    for label, series in [("control", record["control"]), *perturbed.items()]:
        try:
            arr = np.asarray(series, dtype=float)
        except (TypeError, ValueError):
            raise RecordError(f"member {label} is not a numeric grid") from None
        if arr.shape != shape or not np.isfinite(arr).all():
            raise RecordError(f"member {label} must be finite values of shape {shape}")


def encode_record(record: dict) -> bytes:
    validate_record(record)
    return gzip.compress(encode(record), compresslevel=9, mtime=0)


def decode_record(data: bytes) -> dict:
    try:
        text = gzip.decompress(data)
        record = json.loads(text)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RecordError(f"not a gzip JSON record: {e}") from None
    validate_record(record)
    if encode(record) != text:
        raise RecordError("record JSON is not canonical")
    return record


def record_path(source: str, init: datetime) -> str:
    return f"{INPUTS_DIR}/ens-{source}-{init.astimezone(UTC):%Y%m%d%H}.json.gz"


def record_init(record: dict) -> datetime:
    return datetime.strptime(record["init"], _INIT).replace(tzinfo=UTC)


def write_record(ledger_root: Path, record: dict) -> tuple[str, bool]:
    """Save ``record`` write-once; returns (relative path, whether a file was written).

    Saving an identical record again is a no-op. A different record for a saved
    init is refused and the file is left untouched.
    """
    data = encode_record(record)
    rel = record_path(record["source"], record_init(record))
    path = Path(ledger_root) / rel
    if path.exists():
        if path.read_bytes() == data:
            return rel, False
        raise RecordError(f"{rel} already exists with different content; inputs are write-once")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)
    return rel, True


# --- using a record ----------------------------------------------------------


def member_labels(record: dict) -> list[str]:
    """Member order used by ``window_totals``: the control, then perturbed by number."""
    return ["control", *sorted(record["perturbed"], key=int)]


def member_count(record: dict) -> int:
    """n: the control plus every perturbed member."""
    return 1 + len(record["perturbed"])


def window_totals(record: dict, start_step: int, end_step: int) -> np.ndarray:
    """Rain (mm) between two saved steps, per member: shape (n, lats, lons).

    Accumulation from ``start_step`` to ``end_step`` is the difference of the
    cumulative values. Both steps must be in the record.
    """
    steps = record["steps"]
    if start_step not in steps or end_step not in steps or end_step < start_step:
        raise RecordError(f"steps {start_step}-{end_step} h are not a window of this record")
    i, j = steps.index(start_step), steps.index(end_step)
    series = [record["control"], *(record["perturbed"][k] for k in member_labels(record)[1:])]
    stacked = np.asarray(series, dtype=float)
    return stacked[:, j] - stacked[:, i]

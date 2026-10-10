"""Band map and the one band-mean function (KTD4, R17). Pure; frozen at the bundle tag (KTD9).

A band map says, for every IMD 0.25° lattice cell that holds part of the
district, how much of the district lies in it (its weight) and which verdict
band the cell reports to. ``fetch_bands`` reads the district polygon and the
DEM and hands rasters to ``tally``; ``build`` turns the tallies into the map.

Candidate bands are elevation zones 1-3; zone 4, above them, is tallied so
every district pixel is accounted for, but it is never a band of its own. A
cell's band is the zone covering most of its district area. Bands whose
district area falls in a common cell share that cell's observation and merge
into one verdict band. Bands count as independent only with distinct gauges
behind them, so without gauge evidence, or with fewer than two verdict bands
left, the map is one district-wide band and says why.

Zone edges are not registered: the map file holds no zone or band column. The
default edges are the Himachal Pradesh Department of Agriculture's
(agriculture.hp.gov.in/?p=3940): zone 1 up to 1000 m, zone 2 to 1500 m, zone 3
to 2500 m. ``docs/findings/2026-10-mandi-elevation-zones.md`` compares them
with other published edges; the verdict band is the same under each.

On disk the map is ``bands/band-map.csv``: fixed columns, fixed decimals, cells
ascending by latitude then longitude, LF line ends. The same map always gives
the same bytes, and a file that does not re-encode to itself is refused.

``band_mean`` is the only place a cell field becomes a band value. Forecast
fields and observed fields both go through it, with the same coverage rule.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

LATTICE = 0.25  # IMD gridded rainfall: 0.25° cells centred on multiples of 0.25°
BAND_MAP_PATH = "bands/band-map.csv"

ZONES = ("zone-1", "zone-2", "zone-3", "zone-4")
ZONE_UPPER_M = (1000.0, 1500.0, 2500.0)  # inclusive upper edge of zones 1-3
CANDIDATES = ZONES[:3]

DISTRICT = "district"
NO_INDEPENDENT_GAUGES = "no-independent-gauges"
FEWER_THAN_TWO_BANDS = "fewer-than-two-bands"
REASONS = ("", NO_INDEPENDENT_GAUGES, FEWER_THAN_TWO_BANDS)

COLUMNS = ("lat", "lon", "district_km2", "weight", "verdict_band", "reason")

# WGS84
_A_KM = 6378.137
_E2 = 0.00669437999014


class BandMapError(ValueError):
    """A band map cannot be built, read or used as asked."""


@dataclass(frozen=True)
class Raster:
    """Elevation on a regular grid with pixel centres at whole multiples of 1/``per_degree``°.

    Row 0 is the northernmost; its centre is at ``north / per_degree``° and
    column 0's at ``west / per_degree``°. ``inside`` marks the pixels whose
    centre lies in the district.
    """

    per_degree: int
    north: int
    west: int
    elevation: np.ndarray  # metres, shape (rows, cols)
    inside: np.ndarray  # bool, same shape


@dataclass(frozen=True)
class Cell:
    lat: float  # cell centre
    lon: float
    district_km2: float
    weight: float  # share of the district's area
    verdict_band: str


@dataclass(frozen=True)
class BandMap:
    cells: tuple[Cell, ...]
    reason: str  # why the verdict bands are what they are; one of REASONS

    @property
    def verdict_bands(self) -> tuple[str, ...]:
        return tuple(sorted({c.verdict_band for c in self.cells} - {""}))


@dataclass(frozen=True)
class BandMean:
    """Band means over the leading axes of a field; NaN where coverage is short."""

    values: np.ndarray
    coverage: np.ndarray  # share of the band's weight on cells with a value
    sufficient: np.ndarray  # coverage >= the coverage share asked for


# --- tallying district area per cell and zone -------------------------------


def _cell(index: np.ndarray, per_degree: int) -> np.ndarray:
    """Lattice index of the 0.25° cell holding each pixel centre, in whole-number arithmetic."""
    return (8 * index + per_degree) // (2 * per_degree)


def _zone_q(lat_deg: np.ndarray) -> np.ndarray:
    e = math.sqrt(_E2)
    s = np.sin(np.radians(lat_deg))
    return s / (1 - _E2 * s * s) + np.arctanh(e * s) / e


def strip_km2(south: np.ndarray, north: np.ndarray, width_deg: float) -> np.ndarray:
    """Area on the WGS84 ellipsoid between two latitudes, ``width_deg`` of longitude wide."""
    b2 = _A_KM * _A_KM * (1 - _E2)
    return b2 / 2 * math.radians(width_deg) * (_zone_q(north) - _zone_q(south))


def tally(raster: Raster, zone_upper_m: Sequence[float] = ZONE_UPPER_M) -> dict[tuple[int, int], np.ndarray]:
    """District km² per lattice cell and zone: {(lat index, lon index): area per ZONES}.

    ``zone_upper_m`` holds the inclusive upper edges of zones 1-3.
    """
    elevation = np.asarray(raster.elevation, dtype=float)
    inside = np.asarray(raster.inside, dtype=bool)
    if elevation.ndim != 2 or inside.shape != elevation.shape:
        raise BandMapError("elevation and inside must be 2-D arrays of one shape")
    if not np.isfinite(elevation[inside]).all():
        raise BandMapError("the DEM has no elevation for a pixel inside the district")
    rows, cols = elevation.shape
    lat = (raster.north - np.arange(rows)) / raster.per_degree
    half = 0.5 / raster.per_degree
    row_km2 = strip_km2(lat - half, lat + half, 1 / raster.per_degree)
    cell_lat = _cell(raster.north - np.arange(rows), raster.per_degree)
    cell_lon = _cell(raster.west + np.arange(cols), raster.per_degree)
    zone = np.searchsorted(zone_upper_m, elevation, side="left")
    areas = {}
    for i in np.unique(cell_lat):
        r = cell_lat == i
        for j in np.unique(cell_lon):
            c = cell_lon == j
            km2 = (row_km2[r][:, None] * inside[np.ix_(r, c)]).ravel()
            by_zone = np.bincount(zone[np.ix_(r, c)].ravel(), weights=km2, minlength=len(ZONES))
            if by_zone.any():
                areas[int(i), int(j)] = by_zone
    return areas


# --- building the map --------------------------------------------------------


def build(tallies: Iterable[dict[tuple[int, int], np.ndarray]], *,
          independent_gauges: bool = False) -> BandMap:
    """The band map from every raster's tally.

    ``independent_gauges`` says whether distinct gauges are known to stand behind
    each verdict band (R17). IMD publishes no gauge list per cell, so it is False.
    """
    areas: dict[tuple[int, int], np.ndarray] = {}
    for one in tallies:
        for key, by_zone in one.items():
            areas[key] = areas.get(key, 0.0) + by_zone
    total = sum(float(a.sum()) for a in areas.values())
    if not total > 0:
        raise BandMapError("no district area on any raster")

    merged = _merge_shared(areas.values())
    band = {key: majority_band(by_zone) for key, by_zone in areas.items()}
    reason = ""
    if not independent_gauges:
        reason = NO_INDEPENDENT_GAUGES
    elif len({merged[b] for b in band.values() if b in merged}) < 2:
        reason = FEWER_THAN_TWO_BANDS

    cells = []
    for (i, j), by_zone in sorted(areas.items()):
        km2 = float(by_zone.sum())
        cells.append(Cell(
            lat=i * LATTICE, lon=j * LATTICE, district_km2=km2, weight=km2 / total,
            verdict_band=DISTRICT if reason else merged.get(band[i, j], ""),
        ))
    return _rounded(BandMap(cells=tuple(cells), reason=reason))


def majority_band(by_zone: np.ndarray) -> str:
    """The zone holding most of a cell's district area; ties go to the lower zone."""
    return ZONES[int(np.argmax(by_zone))]


def _merge_shared(areas: Iterable[np.ndarray]) -> dict[str, str]:
    """Verdict band of each candidate band, after merging bands that share a cell."""
    group = {band: {band} for band in CANDIDATES}
    for by_zone in areas:
        present = [b for b, a in zip(CANDIDATES, by_zone) if a > 0]
        union = set().union(*(group[b] for b in present))
        for band in union:
            group[band] = union
    return {band: "+".join(sorted(members)) for band, members in group.items()}


def _rounded(band_map: BandMap) -> BandMap:
    """The map as its file holds it, so a built map equals the one read back."""
    return decode(encode(band_map, check=False))


# --- reading and writing -----------------------------------------------------


def encode(band_map: BandMap, *, check: bool = True) -> bytes:
    lines = [",".join(COLUMNS)]
    for c in band_map.cells:
        lines.append(",".join([
            f"{c.lat:.2f}", f"{c.lon:.2f}", f"{c.district_km2:.3f}", f"{c.weight:.6f}",
            c.verdict_band, band_map.reason,
        ]))
    data = ("\n".join(lines) + "\n").encode()
    if check:
        decode(data)
    return data


def decode(data: bytes) -> BandMap:
    """Parse ``band-map.csv`` strictly; anything but a canonical, consistent map is refused."""
    try:
        header, *rows = data.decode("ascii").split("\n")
    except UnicodeDecodeError:
        raise BandMapError("band map is not ASCII text") from None
    if header != ",".join(COLUMNS) or not rows or rows.pop() != "":
        raise BandMapError(f"band map must start with the header {','.join(COLUMNS)} and end with a newline")
    cells, reasons = [], set()
    for n, row in enumerate(rows, start=2):
        fields = row.split(",")
        if len(fields) != len(COLUMNS):
            raise BandMapError(f"line {n}: expected {len(COLUMNS)} fields")
        try:
            lat, lon, km2, weight = (float(x) for x in fields[:4])
        except ValueError:
            raise BandMapError(f"line {n}: not a number") from None
        verdict_band, reason = fields[4:]
        reasons.add(reason)
        cells.append(Cell(lat, lon, km2, weight, verdict_band))
    band_map = BandMap(cells=tuple(cells), reason=reasons.pop() if len(reasons) == 1 else "?")
    _check(band_map)
    if encode(band_map, check=False) != data:
        raise BandMapError("band map is not in canonical form")
    return band_map


def _check(m: BandMap) -> None:
    if not m.cells:
        raise BandMapError("band map has no cells")
    if m.reason not in REASONS:
        raise BandMapError("every row must carry the same known reason")
    keys = [(c.lat, c.lon) for c in m.cells]
    if keys != sorted(set(keys)):
        raise BandMapError("cells must be distinct and ascending by latitude then longitude")
    for c in m.cells:
        if any(abs(x / LATTICE - round(x / LATTICE)) > 1e-9 for x in (c.lat, c.lon)):
            raise BandMapError(f"cell {c.lat}, {c.lon} is off the IMD {LATTICE}° lattice")
        if not (c.district_km2 > 0 and 0 <= c.weight <= 1):
            raise BandMapError(f"cell {c.lat}, {c.lon} must hold district area and a weight in 0-1")
        if m.reason and c.verdict_band != DISTRICT:
            raise BandMapError(f"a map with a reason has only the {DISTRICT} verdict band")
    if abs(sum(c.weight for c in m.cells) - 1) > 1e-4:
        raise BandMapError("weights must sum to 1")
    if not m.verdict_bands:
        raise BandMapError("band map has no verdict band")


# --- the band mean -----------------------------------------------------------


def band_mean(band_map: BandMap, verdict_band: str, lats: Sequence[float], lons: Sequence[float],
              field, coverage_share: float) -> BandMean:
    """Area-weighted mean of ``field`` over a verdict band's cells.

    ``field`` has shape (..., len(lats), len(lons)); the mean is taken over the
    last two axes. ``lats`` and ``lons`` are cell centres on the IMD lattice, in
    any order. A cell counts as covered when it is on the grid and its value
    is finite. The mean uses the covered cells only, and is NaN wherever they
    hold less than ``coverage_share`` of the band's weight.
    """
    cells = [c for c in band_map.cells if c.verdict_band == verdict_band]
    if not cells:
        raise BandMapError(f"no verdict band {verdict_band!r} in the band map")
    values = np.asarray(field, dtype=float)
    if values.shape[-2:] != (len(lats), len(lons)):
        raise BandMapError(f"field shape {values.shape} does not end in the grid's shape")
    for x in (*lats, *lons):
        if not math.isfinite(x) or abs(x / LATTICE - round(x / LATTICE)) > 1e-9:
            raise BandMapError(f"grid coordinate {x} is off the IMD {LATTICE}° lattice")
    row = {round(lat / LATTICE): i for i, lat in enumerate(lats)}
    col = {round(lon / LATTICE): j for j, lon in enumerate(lons)}
    if len(row) != len(lats) or len(col) != len(lons):
        raise BandMapError("grid coordinates must be distinct")
    weights = np.zeros((len(lats), len(lons)))
    total = off_grid = 0.0
    for c in cells:
        total += c.weight
        i, j = row.get(round(c.lat / LATTICE)), col.get(round(c.lon / LATTICE))
        if i is None or j is None:
            off_grid += c.weight
        else:
            weights[i, j] = c.weight
    valid = np.isfinite(values)
    # Counting what is missing keeps full coverage at exactly 1.
    missing = off_grid + (weights * ~valid).sum(axis=(-2, -1))
    coverage = 1 - missing / total
    sufficient = coverage >= coverage_share
    covered = (weights * valid).sum(axis=(-2, -1))
    summed = (weights * np.where(valid, values, 0.0)).sum(axis=(-2, -1))
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(sufficient & (covered > 0), summed / covered, np.nan)
    return BandMean(values=mean, coverage=coverage, sufficient=sufficient)

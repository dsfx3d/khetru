"""Building the band map from its recorded sources (unfrozen, KTD9). Needs the ``geo`` extra.

Two sources, each pinned by URL and SHA-256 and kept under ``.cache/evidence/bands/``:

- the district polygon: geoBoundaries gbOpen India ADM2 at release ``9469f09``
  (2021 boundaries, ODbL 1.0), the one feature with Mandi's ``shapeID``;
- elevation: the four Copernicus DEM GLO-30 tiles covering it, from the AWS
  open-data bucket ``copernicus-dem-30m``.

A file whose hash differs from the recorded one is refused, so the build reads
the same bytes every time. Each DEM pixel whose centre lies in the polygon is
counted once (``bands.tally``), and ``bands.build`` makes the map.
``ledger/mandi-wheat/bands/band-map.csv`` is write-once: a rebuild must
reproduce it byte for byte.

Zone edges are not registered. ``zone_report`` prints, for each published set
of edges in ``ZONE_SCHEMES``, how the district and each cell divide into zones;
``docs/findings/2026-10-mandi-elevation-zones.md`` holds its output.
"""

import hashlib
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features

from khetru_evidence import bands, forecasts

LEDGER = "mandi-wheat"
CACHE_DIR = "bands"
MANDI_SHAPE_ID = "76128533B23735146933631"

_DEM_URL = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
_EPS = 1e-6


class SourceError(RuntimeError):
    """A source file is missing, altered, or not laid out as the build expects."""


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    sha256: str


def _dem(tile: str, sha256: str) -> Source:
    name = f"Copernicus_DSM_COG_10_{tile}_00_DEM"
    return Source(f"{name}.tif", _DEM_URL.format(name=name), sha256)


POLYGON = Source(
    "geoBoundaries-IND-ADM2.geojson",
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/IND/ADM2/"
    "geoBoundaries-IND-ADM2.geojson",
    "8bef6929fd65432e7dc775e7c44473e84efff064ebddbfd6b834e6291546db40",
)
DEM_TILES = (
    _dem("N31_00_E076", "b9d034362a6324aa242d36670576342334e6c5fb3bcb849a9f7a7563641f4826"),
    _dem("N31_00_E077", "6ee70595fcc9d54c96667be743e42252ba5ea68a2d013b3c4edc4a097e8d1605"),
    _dem("N32_00_E076", "6713f118440ac3760728e25c8f4dbf5953286bea91cfc50fc7a7a6e4939f8389"),
    _dem("N32_00_E077", "d2861f79c130b6f5fbd619a87f544b8f9abdf2abd21995c01d2614961060c044"),
)


# Published edges of Himachal agro-climatic zones 1-3 (inclusive upper edge, m).
ZONE_SCHEMES = {
    # agriculture.hp.gov.in/?p=3940; zone 3 ends at 2500 m where rainfall is at
    # most 1500 mm and at 3250 m where it is more.
    "HP Department of Agriculture, zone 3 to 2500 m": bands.ZONE_UPPER_M,
    "HP Department of Agriculture, zone 3 to 3250 m": (1000.0, 1500.0, 3250.0),
    # Commonly attributed to CSK HPKV; no primary source found.
    "Attributed to CSK HPKV": (650.0, 1800.0, 2200.0),
}
REPORT_MIN_WEIGHT = 0.01  # cells holding less of the district are left out of the report


def obtain(source: Source, cache_dir: Path) -> Path:
    """The source file in the cache, downloaded if absent; its SHA-256 must match."""
    path = Path(cache_dir) / source.name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        with urllib.request.urlopen(source.url) as response, open(part, "wb") as f:
            while chunk := response.read(1 << 20):
                f.write(chunk)
        part.replace(path)
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != source.sha256:
        raise SourceError(f"{path} has SHA-256 {digest.hexdigest()}, recorded {source.sha256}")
    return path


def district_geometry(path: Path, shape_id: str = MANDI_SHAPE_ID) -> dict:
    """The GeoJSON geometry of the one feature with ``shape_id``, in lon/lat degrees."""
    with open(path, encoding="utf-8") as f:
        collection = json.load(f)
    crs = collection.get("crs", {}).get("properties", {}).get("name")
    if crs != "urn:ogc:def:crs:OGC:1.3:CRS84":
        raise SourceError(f"{path}: expected CRS84 lon/lat coordinates, found {crs!r}")
    found = [f for f in collection["features"] if f["properties"].get("shapeID") == shape_id]
    if len(found) != 1:
        raise SourceError(f"{path}: {len(found)} features have shapeID {shape_id}, expected 1")
    return found[0]["geometry"]


def tile_raster(path: Path, geometry: dict) -> bands.Raster:
    """A DEM tile as a ``Raster``, with the pixels whose centre lies in ``geometry`` marked."""
    with rasterio.open(path) as ds:
        t = ds.transform
        per_degree = round(1 / t.a)
        north, west = (t.f + t.e / 2) * per_degree, (t.c + t.a / 2) * per_degree
        regular = (
            ds.crs is not None and ds.crs.to_epsg() == 4326 and t.b == 0 and t.d == 0
            and abs(t.a * per_degree - 1) < _EPS and abs(t.e * per_degree + 1) < _EPS
            and abs(north - round(north)) < _EPS and abs(west - round(west)) < _EPS
        )
        if not regular:
            raise SourceError(f"{path}: not a north-up lon/lat grid with whole-step pixel centres")
        elevation = ds.read(1).astype(float)
        if ds.nodata is not None:
            elevation[elevation == ds.nodata] = np.nan
        inside = rasterio.features.geometry_mask(
            [geometry], out_shape=ds.shape, transform=t, invert=True
        )
    return bands.Raster(per_degree, round(north), round(west), elevation, inside)


def _extent(geometry: dict) -> tuple[float, float, float, float]:
    """(south, north, west, east) of a GeoJSON geometry."""
    west, south, east, north = rasterio.features.bounds(geometry)
    return south, north, west, east


def _require_within(geometry: dict, rasters: list[bands.Raster]) -> None:
    """Refuse a polygon that pokes out of the rasters; its area there would go uncounted."""
    south, north, west, east = _extent(geometry)
    lat_s = min((r.north - r.elevation.shape[0] + 1) / r.per_degree for r in rasters)
    lat_n = max(r.north / r.per_degree for r in rasters)
    lon_w = min(r.west / r.per_degree for r in rasters)
    lon_e = max((r.west + r.elevation.shape[1] - 1) / r.per_degree for r in rasters)
    if not (lat_s <= south and north <= lat_n and lon_w <= west and east <= lon_e):
        raise SourceError("the district polygon reaches beyond the DEM tiles")


def district_rasters(cache_dir: Path) -> list[bands.Raster]:
    """Every DEM tile with the district's pixels marked."""
    geometry = district_geometry(obtain(POLYGON, cache_dir))
    rasters = [tile_raster(obtain(tile, cache_dir), geometry) for tile in DEM_TILES]
    _require_within(geometry, rasters)
    return rasters


def build_band_map(cache_dir: Path) -> bands.BandMap:
    band_map = bands.build(bands.tally(r) for r in district_rasters(cache_dir))
    # Forecast records hold the Mandi box plus a margin; the map must fit inside the box.
    lats, lons = forecasts.box_cells(forecasts.MANDI_BOX, margin=0)
    outside = [c for c in band_map.cells if c.lat not in lats or c.lon not in lons]
    if outside:
        raise SourceError(
            f"cell {outside[0].lat}, {outside[0].lon} holds district area outside forecasts.MANDI_BOX"
        )
    return band_map


def build(*, repo: Path) -> int:
    """Build the band map and save it write-once; 0 if saved or reproduced, 1 if it differs."""
    data = bands.encode(build_band_map(repo / ".cache" / "evidence" / CACHE_DIR))
    path = repo / "ledger" / LEDGER / bands.BAND_MAP_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"saved {path.relative_to(repo)}")
        return 0
    if path.read_bytes() == data:
        print(f"reproduced {path.relative_to(repo)} byte for byte")
        return 0
    print(f"{path.relative_to(repo)} differs from the rebuild; the band map is write-once",
          file=sys.stderr)
    return 1


def zone_report(rasters: list[bands.Raster]) -> str:
    """Markdown: the district and its larger cells by zone, under each of ``ZONE_SCHEMES``."""
    lines = []
    for name, edges in ZONE_SCHEMES.items():
        areas: dict[tuple[int, int], np.ndarray] = {}
        for raster in rasters:
            for key, by_zone in bands.tally(raster, edges).items():
                areas[key] = areas.get(key, 0.0) + by_zone
        total = sum(areas.values()).sum()
        with_gauges = bands.build([areas], independent_gauges=True)
        low, mid, high = (f"{e:.0f}" for e in edges)
        lines += [
            f"### {name}", "",
            f"Edges: zone 1 up to {low} m, zone 2 to {mid} m, zone 3 to {high} m, zone 4 above.", "",
            "| Cell (°N, °E) | % of district | Zone 1 | Zone 2 | Zone 3 | Zone 4 | Majority |",
            "|---|---|---|---|---|---|---|",
        ]
        for (i, j), by_zone in sorted(areas.items()):
            if by_zone.sum() / total < REPORT_MIN_WEIGHT:
                continue
            shares = " | ".join(f"{100 * a / by_zone.sum():.1f}" for a in by_zone)
            lines.append(
                f"| {i * bands.LATTICE:.2f}, {j * bands.LATTICE:.2f} | {100 * by_zone.sum() / total:.1f} "
                f"| {shares} | {bands.majority_band(by_zone)} |"
            )
        district = " | ".join(f"{100 * a / total:.1f}" for a in sum(areas.values()))
        lines += [
            f"| **District** | 100.0 | {district} | |", "",
            f"Verdict bands if independent gauges existed: {', '.join(with_gauges.verdict_bands)}"
            + (f" ({with_gauges.reason})." if with_gauges.reason else "."), "",
        ]
    return "\n".join(lines)


def zones(*, repo: Path) -> int:
    print(zone_report(district_rasters(repo / ".cache" / "evidence" / CACHE_DIR)), end="")
    return 0

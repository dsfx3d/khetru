"""U3: district area per lattice cell and zone, verdict bands, and the band mean.

Rasters are synthetic: 40 pixels per degree over a 3x3 block of 0.25° cells
(centres 31.25-31.75 N, 76.75-77.25 E), 10 pixels per cell each way.
"""

import json

import numpy as np
import pytest

from khetru_evidence import bands as B
from khetru_evidence import cli

PER_DEGREE = 40
NORTH, WEST = 1274, 3065  # top-left pixel centre: 31.85 N, 76.625 E
SHAPE = (30, 30)
LATS, LONS = [31.25, 31.5, 31.75], [76.75, 77.0, 77.25]


def raster(inside, elevation=500.0) -> B.Raster:
    elevation = np.broadcast_to(np.asarray(elevation, dtype=float), SHAPE).copy()
    return B.Raster(PER_DEGREE, NORTH, WEST, elevation, np.asarray(inside, dtype=bool))


def rows(lat: float) -> slice:
    """Pixel rows of the cell centred at ``lat`` (row 0 is the northernmost)."""
    start = round((31.75 - lat) / 0.25) * 10
    return slice(start, start + 10)


def cols(lon: float) -> slice:
    start = round((lon - 76.75) / 0.25) * 10
    return slice(start, start + 10)


def whole_block() -> np.ndarray:
    return np.ones(SHAPE, dtype=bool)


def cell(band_map: B.BandMap, lat: float, lon: float) -> B.Cell:
    return next(c for c in band_map.cells if (c.lat, c.lon) == (lat, lon))


# --- area weights ------------------------------------------------------------


def test_cell_areas_sum_to_the_polygon_area():
    inside = np.zeros(SHAPE, dtype=bool)
    inside[4:27, 3:22] = True  # a rectangle cutting through cells
    # Pixel centre k/40° spans k/40 ± 1/80°, so the rectangle's edges are known.
    south, north = (NORTH - 26) / 40 - 1 / 80, (NORTH - 4) / 40 + 1 / 80
    expected = float(B.strip_km2(np.array(south), np.array(north), 19 / 40))

    band_map = B.build([B.tally(raster(inside))])

    assert sum(c.district_km2 for c in band_map.cells) == pytest.approx(expected, rel=1e-6)
    assert sum(c.weight for c in band_map.cells) == pytest.approx(1.0, abs=1e-5)


def test_strips_cover_the_whole_ellipsoid():
    # The WGS84 ellipsoid's surface is 510,065,622 km².
    assert float(B.strip_km2(np.array(-90.0), np.array(90.0), 360.0)) == pytest.approx(510_065_622, abs=1)


def test_cell_outside_the_polygon_gets_no_weight():
    inside = np.zeros(SHAPE, dtype=bool)
    inside[rows(31.5), cols(77.0)] = True

    band_map = B.build([B.tally(raster(inside))])

    assert [(c.lat, c.lon, c.weight) for c in band_map.cells] == [(31.5, 77.0, 1.0)]
    mean = B.band_mean(band_map, B.DISTRICT, LATS, LONS, np.arange(9.0).reshape(3, 3), 1.0)
    assert mean.values == 4.0  # only the centre cell counts


def test_tallies_from_several_rasters_add_up():
    left, right = np.zeros(SHAPE, dtype=bool), np.zeros(SHAPE, dtype=bool)
    left[rows(31.5), 10:15] = True
    right[rows(31.5), 15:20] = True

    split = B.build([B.tally(raster(left)), B.tally(raster(right))])
    whole = B.build([B.tally(raster(left | right))])

    assert split == whole


def test_missing_elevation_inside_the_district_is_refused():
    elevation = np.full(SHAPE, 500.0)
    elevation[5, 5] = np.nan
    with pytest.raises(B.BandMapError, match="no elevation"):
        B.tally(raster(whole_block(), elevation))


# --- bands and verdict bands -------------------------------------------------


def test_zone_edges_are_inclusive_at_the_top():
    elevation = np.full(SHAPE, 1000.0)
    elevation[rows(31.5), :] = 1000.5
    elevation[rows(31.25), :] = 2500.5
    band_map = B.build([B.tally(raster(whole_block(), elevation))])

    assert cell(band_map, 31.75, 77.0).band == "zone-1"
    assert cell(band_map, 31.5, 77.0).band == "zone-2"
    assert cell(band_map, 31.25, 77.0).band == "zone-4"


def test_cell_split_60_40_goes_to_the_majority_band():
    elevation = np.full(SHAPE, 500.0)
    elevation[:, 4:10] = 1200.0  # 6 of the first cell column's 10 pixel columns
    inside = np.zeros(SHAPE, dtype=bool)
    inside[rows(31.5), cols(76.75)] = True

    band_map = B.build([B.tally(raster(inside, elevation))])

    only = band_map.cells[0]
    assert only.band == "zone-2"
    assert only.zone_shares == pytest.approx((0.4, 0.6, 0.0, 0.0), abs=1e-4)


def test_two_bands_that_share_a_cell_merge_into_one_verdict_band():
    elevation = np.full(SHAPE, 500.0)  # zone 1 in the west
    elevation[:, cols(77.0)] = 1200.0  # zone 2 in the middle column
    elevation[:, cols(77.25)] = 2000.0  # zone 3 in the east
    elevation[rows(31.5), 9] = 1200.0  # ... and a sliver of zone 2 in one western cell

    band_map = B.build([B.tally(raster(whole_block(), elevation))], independent_gauges=True)

    assert band_map.reason == ""
    assert band_map.verdict_bands == ("zone-1+zone-2", "zone-3")
    assert cell(band_map, 31.5, 76.75).verdict_band == "zone-1+zone-2"
    assert cell(band_map, 31.5, 77.0).verdict_band == "zone-1+zone-2"
    assert cell(band_map, 31.5, 77.25).verdict_band == "zone-3"


def test_without_gauge_evidence_the_map_is_one_district_wide_band_with_a_reason():
    elevation = np.full(SHAPE, 500.0)
    elevation[:, cols(77.25)] = 2000.0  # two bands in separate cells

    band_map = B.build([B.tally(raster(whole_block(), elevation))])

    assert band_map.verdict_bands == (B.DISTRICT,)
    assert band_map.reason == B.NO_INDEPENDENT_GAUGES
    assert {c.band for c in band_map.cells} == {"zone-1", "zone-3"}  # bands are still recorded


def test_fewer_than_two_verdict_bands_is_district_wide_even_with_gauges():
    elevation = np.full(SHAPE, 500.0)
    elevation[:, 15:] = 1200.0  # the middle cells hold both bands

    band_map = B.build([B.tally(raster(whole_block(), elevation))], independent_gauges=True)

    assert band_map.verdict_bands == (B.DISTRICT,)
    assert band_map.reason == B.FEWER_THAN_TWO_BANDS


def test_a_cell_mostly_above_the_candidate_bands_has_no_verdict_band_of_its_own():
    elevation = np.full(SHAPE, 500.0)
    elevation[:, cols(77.0)] = 2000.0
    elevation[:, cols(77.25)] = 3000.0

    band_map = B.build([B.tally(raster(whole_block(), elevation))], independent_gauges=True)

    assert band_map.verdict_bands == ("zone-1", "zone-3")
    assert cell(band_map, 31.5, 77.25).verdict_band == ""


# --- the file ----------------------------------------------------------------


def test_band_map_file_round_trips_and_is_stable():
    elevation = np.full(SHAPE, 500.0)
    elevation[:, 4:10] = 1200.0
    band_map = B.build([B.tally(raster(whole_block(), elevation))])

    data = B.encode(band_map)

    assert B.decode(data) == band_map
    assert B.encode(B.decode(data)) == data
    assert data.split(b"\n")[0] == ",".join(B.COLUMNS).encode()


@pytest.mark.parametrize("damage", [
    lambda d: d.replace(b"zone-1,district", b"zone-9,district", 1),  # unknown band
    lambda d: d.replace(b"0.111", b"0.11100", 1),  # not canonical
    lambda d: d.replace(B.NO_INDEPENDENT_GAUGES.encode(), b"because", 1),  # mixed reasons
    lambda d: d.rstrip(b"\n"),  # no final newline
    lambda d: b"\n".join(d.split(b"\n")[:-2]) + b"\n",  # a cell dropped: weights no longer sum to 1
    lambda d: d.replace(b"lat,lon", b"lon,lat", 1),  # wrong header
])
def test_damaged_band_map_file_is_refused(damage):
    data = B.encode(B.build([B.tally(raster(whole_block()))]))
    damaged = damage(data)
    assert damaged != data
    with pytest.raises(B.BandMapError):
        B.decode(damaged)


# --- the band mean -----------------------------------------------------------


@pytest.fixture
def uneven() -> B.BandMap:
    """Three cells in the 31.5 N row holding 50%, 30% and 20% of the district."""
    inside = np.zeros(SHAPE, dtype=bool)
    inside[rows(31.5), 0:10] = True
    inside[rows(31.5), 10:16] = True
    inside[rows(31.5), 20:24] = True
    return B.build([B.tally(raster(inside))])


def field(west: float, middle: float, east: float) -> np.ndarray:
    values = np.full((3, 3), 99.0)  # cells with no district area never count
    values[1] = [west, middle, east]
    return values


def test_band_mean_weights_cells_by_district_area(uneven):
    mean = B.band_mean(uneven, B.DISTRICT, LATS, LONS, field(10, 20, 40), 1.0)

    assert mean.values == pytest.approx(0.5 * 10 + 0.3 * 20 + 0.2 * 40, rel=1e-5)
    assert mean.coverage == 1.0
    assert mean.sufficient


def test_band_mean_keeps_leading_axes(uneven):
    members = np.stack([field(10, 20, 40), field(0, 0, 0), field(1, 1, 1)])

    mean = B.band_mean(uneven, B.DISTRICT, LATS, LONS, members, 1.0)

    assert mean.values == pytest.approx([19.0, 0.0, 1.0], rel=1e-5)
    assert mean.sufficient.all()


def test_short_coverage_is_reported_alike_for_forecast_and_observed_fields(uneven):
    # A forecast grid that stops short of the eastern cell, and an observed
    # field with that cell missing, are the same case.
    forecast = B.band_mean(uneven, B.DISTRICT, LATS, LONS[:2], field(10, 20, 40)[:, :2], 0.9)
    observed = B.band_mean(uneven, B.DISTRICT, LATS, LONS, field(10, 20, np.nan), 0.9)

    for mean in (forecast, observed):
        assert not mean.sufficient
        assert np.isnan(mean.values)
        assert mean.coverage == pytest.approx(0.8, abs=1e-5)


def test_band_mean_above_the_coverage_share_uses_the_covered_cells(uneven):
    mean = B.band_mean(uneven, B.DISTRICT, LATS, LONS, field(10, 20, np.nan), 0.8)

    assert mean.sufficient
    assert mean.values == pytest.approx((0.5 * 10 + 0.3 * 20) / 0.8, rel=1e-5)


def test_coverage_is_judged_per_leading_index(uneven):
    days = np.stack([field(10, 20, 40), field(np.nan, 20, 40)])

    mean = B.band_mean(uneven, B.DISTRICT, LATS, LONS, days, 0.9)

    assert mean.sufficient.tolist() == [True, False]
    assert np.isnan(mean.values[1]) and not np.isnan(mean.values[0])


def test_band_mean_refuses_an_unknown_band_or_a_misshapen_field(uneven):
    with pytest.raises(B.BandMapError, match="no verdict band"):
        B.band_mean(uneven, "zone-1", LATS, LONS, field(1, 1, 1), 1.0)
    with pytest.raises(B.BandMapError, match="shape"):
        B.band_mean(uneven, B.DISTRICT, LATS, LONS, np.zeros((2, 3)), 1.0)


# --- building from source files (needs the geo extra) ------------------------


@pytest.fixture
def geo(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from khetru_evidence import fetch_bands as fb

    elevation = np.full(SHAPE, 500.0, dtype="float32")
    elevation[:, 15:] = 1200.0
    step = 1 / PER_DEGREE
    tile = tmp_path / "dem.tif"
    with rasterio.open(
        tile, "w", driver="GTiff", height=SHAPE[0], width=SHAPE[1], count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(WEST * step - step / 2, NORTH * step + step / 2, step, step),
    ) as ds:
        ds.write(elevation, 1)
    # A rectangle whose edges fall on pixel edges, so its raster area is exact.
    west, east, south, north = 76.7125, 77.1875, 31.2125, 31.7875
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    polygon = tmp_path / "districts.geojson"
    polygon.write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [
            {"type": "Feature", "properties": {"shapeID": "elsewhere"},
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
            {"type": "Feature", "properties": {"shapeID": "here"},
             "geometry": {"type": "Polygon", "coordinates": [ring]}},
        ],
    }))
    area = float(B.strip_km2(np.array(south), np.array(north), east - west))
    return fb, tile, polygon, area


def test_tile_and_polygon_files_give_the_polygon_area(geo):
    fb, tile, polygon, area = geo

    geometry = fb.district_geometry(polygon, "here")
    band_map = B.build([B.tally(fb.tile_raster(tile, geometry))])

    assert sum(c.district_km2 for c in band_map.cells) == pytest.approx(area, rel=1e-6)
    assert cell(band_map, 31.5, 76.75).band == "zone-1"
    assert cell(band_map, 31.5, 77.25).band == "zone-2"


def test_polygon_must_be_the_one_feature_with_the_shape_id(geo):
    fb, _, polygon, _ = geo
    with pytest.raises(fb.SourceError, match="0 features"):
        fb.district_geometry(polygon, "nowhere")


def test_source_with_another_hash_is_refused(geo, tmp_path):
    fb, tile, _, _ = geo
    with pytest.raises(fb.SourceError, match="SHA-256"):
        fb.obtain(fb.Source(tile.name, "unused://", "0" * 64), tmp_path)


def test_polygon_beyond_the_tiles_is_refused(geo):
    fb, tile, polygon, _ = geo
    geometry = fb.district_geometry(polygon, "here")
    wide = {"type": "Polygon", "coordinates": [[[76.0, 31.3], [77.0, 31.3], [77.0, 31.6], [76.0, 31.3]]]}
    with pytest.raises(fb.SourceError, match="beyond the DEM"):
        fb._require_within(wide, [fb.tile_raster(tile, geometry)])


def test_rebuild_must_reproduce_the_saved_band_map(geo, tmp_path, monkeypatch, capsys):
    fb, _, _, _ = geo
    inside = np.zeros(SHAPE, dtype=bool)
    inside[rows(31.5), cols(77.0)] = True
    built = [B.build([B.tally(raster(inside))])]
    monkeypatch.setattr(fb, "build_band_map", lambda cache_dir: built[0])

    assert cli.main(["bands", "build", "--repo", str(tmp_path)]) == 0
    saved = tmp_path / "ledger" / fb.LEDGER / B.BAND_MAP_PATH
    first = saved.read_bytes()
    assert cli.main(["bands", "build", "--repo", str(tmp_path)]) == 0

    built[0] = B.build([B.tally(raster(whole_block()))])
    assert cli.main(["bands", "build", "--repo", str(tmp_path)]) == 1
    assert saved.read_bytes() == first
    assert "write-once" in capsys.readouterr().err


def test_committed_band_map_is_valid_and_district_wide():
    from pathlib import Path

    path = Path(__file__).parents[3] / "ledger" / "mandi-wheat" / B.BAND_MAP_PATH
    band_map = B.decode(path.read_bytes())

    assert band_map.verdict_bands == (B.DISTRICT,)
    assert band_map.reason == B.NO_INDEPENDENT_GAUGES

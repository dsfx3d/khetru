# Band map provenance

`band-map.csv` maps IMD 0.25° lattice cells to their share of Mandi district
and to verdict bands (plan unit U3, KTD4, R17). It is write-once. Built on
2026-10-10.

**Elevation zones are not registered.** The file holds no zone or band column,
and no zone edges are fixed by it. See "Elevation zones" below.

## Rebuild

```
uv sync --all-packages --all-extras
uv run --all-packages evidence bands build
```

The command downloads the sources below into `.cache/evidence/bands/`, refuses
any file whose SHA-256 differs from the recorded one, and rebuilds the map. It
prints `reproduced ... byte for byte` and exits 0 when the result equals the
committed file, and exits 1 when it differs. Checked on 2026-10-10 from an empty
cache.

- `band-map.csv` SHA-256: `3b7d40b946de3cb7053954a77801f3d22e2d1935470ef66f930ae8b52a02763d`
- Built with Python 3.13, numpy 2.5.3, rasterio 1.5.2 (GDAL 3.12.2), as pinned in `uv.lock`.

## Sources

The recorded URLs and hashes live in `py/evidence/src/khetru_evidence/fetch_bands.py`.

### District polygon

- geoBoundaries gbOpen, India ADM2, release `9469f09` (2021 boundaries; build date 12 Dec 2023).
- Licence: Open Data Commons Open Database License 1.0. Source named by geoBoundaries: Pathways Data Pvt. Ltd., lgdirectory.gov.in.
- URL: https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/IND/ADM2/geoBoundaries-IND-ADM2.geojson
- SHA-256: `8bef6929fd65432e7dc775e7c44473e84efff064ebddbfd6b834e6291546db40`
- Feature: `shapeName` "Mandi", `shapeID` `76128533B23735146933631`. The build selects by `shapeID` and fails unless exactly one feature has it.

### Elevation

Copernicus DEM GLO-30 (DSM, 1 arc-second, cloud-optimised GeoTIFF) from the AWS
open-data bucket `copernicus-dem-30m`. The bucket does not state a product
release; the objects were last modified on 9 May 2022.

URL pattern: `https://copernicus-dem-30m.s3.amazonaws.com/<name>/<name>.tif`

| Tile (`<name>`) | SHA-256 |
|---|---|
| `Copernicus_DSM_COG_10_N31_00_E076_00_DEM` | `b9d034362a6324aa242d36670576342334e6c5fb3bcb849a9f7a7563641f4826` |
| `Copernicus_DSM_COG_10_N31_00_E077_00_DEM` | `6ee70595fcc9d54c96667be743e42252ba5ea68a2d013b3c4edc4a097e8d1605` |
| `Copernicus_DSM_COG_10_N32_00_E076_00_DEM` | `6713f118440ac3760728e25c8f4dbf5953286bea91cfc50fc7a7a6e4939f8389` |
| `Copernicus_DSM_COG_10_N32_00_E077_00_DEM` | `d2861f79c130b6f5fbd619a87f544b8f9abdf2abd21995c01d2614961060c044` |

Attribution: produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and
© Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the
European Union and ESA; all rights reserved.

## Method

1. Each DEM pixel whose centre lies inside the polygon counts as district area. Its area is computed on the WGS84 ellipsoid.
2. A pixel belongs to the 0.25° cell holding its centre.
3. A cell's `weight` is its share of the district's area.
4. Bands count as independent only when distinct gauges stand behind them (R17). IMD publishes no list of gauges per cell, so the map is built without gauge evidence and has one verdict band, `district`.

## Elevation zones

The build also sorts each pixel into an elevation zone, because bands that
share a cell must merge (R17). The zones do not reach this file: with no gauge
evidence the map is district-wide whatever the zones are.

`docs/findings/2026-10-mandi-elevation-zones.md` records the owner's decision
of 2026-10-10 to leave zones out, the published zone edges that were compared,
and the result: one verdict band under each of them. A zone scheme, if one is
ever registered, goes in a new file here.

## Result

- 14 cells hold district area. 10 hold 1% or more of it. This matches the U1 finding.
- District area: 3954.3 km², against the official 3950 km².
- **One verdict band, `district`. Reason recorded in the file: `no-independent-gauges`.**

## Columns

| Column | Meaning |
|---|---|
| `lat`, `lon` | Cell centre in degrees |
| `district_km2` | District area inside the cell |
| `weight` | That area as a share of the district; the band-mean weight |
| `verdict_band` | The band the cell reports to |
| `reason` | Why the verdict bands are what they are: `no-independent-gauges`, `fewer-than-two-bands`, or empty |

`khetru_evidence.bands.band_mean` turns a field on these cells into a band
value, for forecasts and observations alike.

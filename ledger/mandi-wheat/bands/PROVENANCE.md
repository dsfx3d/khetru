# Band map provenance

`band-map.csv` maps IMD 0.25° lattice cells to elevation bands and verdict
bands for Mandi district (plan unit U3, KTD4, R17). It is write-once. Built on
2026-10-10.

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

- `band-map.csv` SHA-256: `2a1cc9671e3f4ff05be3b57001ff07eb019d98fd9cda5b90e59c10d5ab051741`
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

### Elevation bands

The Himachal Pradesh agro-climatic zones as published by the state Department
of Agriculture (https://agriculture.hp.gov.in/?p=3940, read 2026-10-10):

| Band | Zone | Elevation |
|---|---|---|
| `zone-1` | Sub-tropical sub-montane and low hills | up to 1000 m |
| `zone-2` | Sub-humid mid hills | 1001–1500 m |
| `zone-3` | Wet temperate high hills | 1501–2500 m |
| `zone-4` | Dry temperate high hills (not a candidate band) | above 2500 m |

Two choices were made here:

- The page gives zone 3 two upper edges: 2500 m where rainfall is at most 1500 mm and 3250 m where it is more. 2500 m is used.
- Other sources give other edges. The edges often cited from CSK HPKV are 650, 1800 and 2200 m; no primary source for them was found on 2026-10-10.

Neither choice changes the verdict bands below. They change only the `band` and
`zone_*` columns.

## Method

1. Each DEM pixel whose centre lies inside the polygon counts as district area. Its area is computed on the WGS84 ellipsoid.
2. A pixel belongs to the 0.25° cell holding its centre and to the zone holding its elevation. Upper edges are inclusive.
3. A cell's `weight` is its share of the district's area. Its `band` is the zone covering most of its district area.
4. Candidate bands (zones 1–3) that both have area in one cell share that cell's observation and merge into one verdict band (R17).
5. Bands count as independent only when distinct gauges stand behind them. IMD publishes no list of gauges per cell, so the map is built without gauge evidence and has one verdict band, `district`.

## Result

- 14 cells hold district area. 10 hold 1% or more of it. This matches the U1 finding.
- District area: 3954.3 km², against the official 3950 km².
- District area by zone: zone 1 20.3%, zone 2 33.0%, zone 3 37.9%, zone 4 8.8%.
- **One verdict band, `district`. Reason recorded in the file: `no-independent-gauges`.**
- The map would be one band even with gauge evidence. Each of the 10 larger cells holds at least two candidate zones, and most hold all three, so merging shared cells already joins zones 1–3.

## Columns

| Column | Meaning |
|---|---|
| `lat`, `lon` | Cell centre in degrees |
| `district_km2` | District area inside the cell |
| `weight` | That area as a share of the district; the band-mean weight |
| `zone_1` … `zone_4` | Share of the cell's district area in each zone |
| `band` | The zone with the largest share |
| `verdict_band` | The band the cell reports to |
| `reason` | Why the verdict bands are what they are: `no-independent-gauges`, `fewer-than-two-bands`, or empty |

`khetru_evidence.bands.band_mean` turns a field on these cells into a band
value, for forecasts and observations alike.

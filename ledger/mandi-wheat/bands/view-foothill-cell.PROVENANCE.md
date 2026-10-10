# Foothill cell view provenance

`view-foothill-cell.csv` holds one IMD 0.25° lattice cell, centred 32.00°N,
76.75°E, as an exploratory view band, `view-foothill-cell`. It is write-once.
Added on 2026-10-11.

**The view is not a verdict band.** `band-map.csv` is unchanged and stays the
registered map, with its one verdict band `district`. Claims and scores for the
view are always `exploratory`. `docs/findings/2026-10-foothill-cell-exploratory-view.md`
says what the view is for and what its results may not be used for.

- `view-foothill-cell.csv` SHA-256: `52af4195530500eb22c5c6c3ef5439b06026d5a156ab1a3cf4573aa8a4260e04`
- `band-map.csv` SHA-256 when the view was added: `3b7d40b946de3cb7053954a77801f3d22e2d1935470ef66f930ae8b52a02763d`

## Source

The file has no source of its own. Its one row is the `32.00,76.75` row of
`band-map.csv`, with two changes:

| Column | In `band-map.csv` | In `view-foothill-cell.csv` |
|---|---|---|
| `weight` | `0.090245`, the cell's share of the district | `1.000000`, the cell is the whole view |
| `verdict_band` | `district` | `view-foothill-cell` |
| `reason` | `no-independent-gauges` | empty |

`lat`, `lon` and `district_km2` are copied. `PROVENANCE.md` gives the sources
and method behind them.

## Check

```
uv run --all-packages pytest py/evidence/tests/test_views.py
```

The tests fail unless the file equals those bytes, its cell and `district_km2`
match the row in `band-map.csv`, and both files have the hashes recorded here
and in `PROVENANCE.md`. No download is needed.

## What the view covers

- **The whole cell, not only its part inside Mandi.** The view's value is the
  IMD value of the cell, for observations, and the forecast value on the same
  cell. With one cell the weight is 1 whatever area is counted, so no boundary
  enters any number.
- The cell runs from 31.875°N to 32.125°N and 76.625°E to 76.875°E, 654.9 km²
  on the WGS84 ellipsoid. `district_km2` says 356.850 km² of it, 54%, lies
  inside Mandi district; the rest is in the neighbouring district. The column
  keeps the meaning it has in `band-map.csv` and is not used as a weight.
- `reason` explains why the verdict bands of a map are what they are. A view
  has no verdict band, so it is empty.

## Format

The format and columns are those of `band-map.csv`, and
`khetru_evidence.bands.decode` reads both. `khetru_evidence.views` loads every
`bands/view-*.csv` and refuses one that does not hold exactly the band it is
named after. A band whose name starts with `view-` is a view; the ledger
refuses an entry for one under any kind but `exploratory`.

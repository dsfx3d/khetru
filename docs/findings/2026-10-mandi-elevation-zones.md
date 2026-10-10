# Mandi elevation zones: why the band map registers none

- **Date:** 2026-10-10
- **Status:** Owner decision recorded on 2026-10-10 (see Decision).
- **Plan unit:** U3 in `docs/plans/2026-10-08-1554-feat-mandi-wheat-claim-ledger-plan.md`

## Finding

Mandi's agro-climatic zones cannot be separated on the IMD 0.25° grid, whichever published zone edges are used. The verdict band is district-wide under every scheme checked. The label of a cell's majority zone, by contrast, depends heavily on the scheme: it differs in 8 of the 10 larger cells between the Department of Agriculture's edges and those attributed to CSK HPKV.

So `ledger/mandi-wheat/bands/band-map.csv` registers cell weights and the verdict band only. It holds no zone or band column, and no zone edges are registered.

## Decision

Approved by the owner on 2026-10-10, after a council review of PR #6:

- The band map keeps `lat`, `lon`, `district_km2`, `weight`, `verdict_band` and `reason`. The `band` and `zone_*` columns are left out.
- This departs from the plan's wording for U3 ("a map from IMD cells to bands to verdict bands"). The code still classifies elevation and merges bands that share cells; only the written file leaves the bands out.
- Zone edges are chosen when per-band scoring against gauges is built. A zone scheme then goes in a new file under `bands/`.

Reasons:

- Zone edges change no weight, no verdict band and no score.
- The Department of Agriculture's table gives zone 3 two upper edges (2500 m and 3250 m, by rainfall), so using it means picking one.
- The 650 / 1800 / 2200 m edges attributed to CSK HPKV, which the plan treats as the agronomic authority, had no primary source in two searches.
- `bands/` is write-once, so a frozen label that later proved wrong could only be superseded, never corrected.

## Zone edges checked

| Scheme | Zone 1 up to | Zone 2 up to | Zone 3 up to | Source |
|---|---|---|---|---|
| HP Department of Agriculture | 1000 m | 1500 m | 2500 m (rainfall at most 1500 mm) or 3250 m (more) | https://agriculture.hp.gov.in/?p=3940, read 2026-10-10 |
| Attributed to CSK HPKV | 650 m | 1800 m | 2200 m | No primary source found. The Mandi district contingency plan gives the district's altitude as 651 to 4000 m and places it in the mid-hills sub-humid zone, which fits a 650 m edge |

Zones 1 to 3 are the candidate bands: sub-montane and low hills, mid hills, and wet temperate high hills. Zone 4 is everything above.

## District and cells by zone

The tables below are the output of `uv run --all-packages evidence bands zones` (needs `uv sync --all-packages --all-extras`). It reads the same pinned polygon and DEM tiles as the band map; `ledger/mandi-wheat/bands/PROVENANCE.md` lists them. Cells holding under 1% of the district are left out. Zone columns are percentages of the cell's district area.

### HP Department of Agriculture, zone 3 to 2500 m

Edges: zone 1 up to 1000 m, zone 2 to 1500 m, zone 3 to 2500 m, zone 4 above.

| Cell (°N, °E) | % of district | Zone 1 | Zone 2 | Zone 3 | Zone 4 | Majority |
|---|---|---|---|---|---|---|
| 31.25, 77.00 | 4.8 | 17.8 | 41.0 | 41.2 | 0.0 | zone-3 |
| 31.25, 77.25 | 7.0 | 12.0 | 34.9 | 53.1 | 0.0 | zone-3 |
| 31.50, 76.75 | 6.2 | 47.4 | 46.5 | 6.1 | 0.0 | zone-1 |
| 31.50, 77.00 | 16.6 | 19.3 | 27.8 | 47.7 | 5.2 | zone-3 |
| 31.50, 77.25 | 13.7 | 0.0 | 5.9 | 61.9 | 32.2 | zone-3 |
| 31.75, 76.75 | 14.0 | 48.4 | 42.4 | 9.3 | 0.0 | zone-1 |
| 31.75, 77.00 | 15.9 | 20.3 | 48.0 | 29.6 | 2.2 | zone-2 |
| 31.75, 77.25 | 6.5 | 1.6 | 25.4 | 56.6 | 16.4 | zone-3 |
| 32.00, 76.75 | 9.0 | 24.9 | 47.4 | 25.3 | 2.4 | zone-2 |
| 32.00, 77.00 | 5.9 | 0.5 | 10.0 | 57.4 | 32.0 | zone-3 |
| **District** | 100.0 | 20.3 | 33.0 | 37.9 | 8.8 | |

Verdict bands if independent gauges existed: district (fewer-than-two-bands).

### HP Department of Agriculture, zone 3 to 3250 m

Edges: zone 1 up to 1000 m, zone 2 to 1500 m, zone 3 to 3250 m, zone 4 above.

| Cell (°N, °E) | % of district | Zone 1 | Zone 2 | Zone 3 | Zone 4 | Majority |
|---|---|---|---|---|---|---|
| 31.25, 77.00 | 4.8 | 17.8 | 41.0 | 41.2 | 0.0 | zone-3 |
| 31.25, 77.25 | 7.0 | 12.0 | 34.9 | 53.1 | 0.0 | zone-3 |
| 31.50, 76.75 | 6.2 | 47.4 | 46.5 | 6.1 | 0.0 | zone-1 |
| 31.50, 77.00 | 16.6 | 19.3 | 27.8 | 52.8 | 0.0 | zone-3 |
| 31.50, 77.25 | 13.7 | 0.0 | 5.9 | 94.1 | 0.1 | zone-3 |
| 31.75, 76.75 | 14.0 | 48.4 | 42.4 | 9.3 | 0.0 | zone-1 |
| 31.75, 77.00 | 15.9 | 20.3 | 48.0 | 31.8 | 0.0 | zone-2 |
| 31.75, 77.25 | 6.5 | 1.6 | 25.4 | 73.1 | 0.0 | zone-3 |
| 32.00, 76.75 | 9.0 | 24.9 | 47.4 | 27.7 | 0.0 | zone-2 |
| 32.00, 77.00 | 5.9 | 0.5 | 10.0 | 83.8 | 5.6 | zone-3 |
| **District** | 100.0 | 20.3 | 33.0 | 46.4 | 0.3 | |

Verdict bands if independent gauges existed: district (fewer-than-two-bands).

### Attributed to CSK HPKV

Edges: zone 1 up to 650 m, zone 2 to 1800 m, zone 3 to 2200 m, zone 4 above.

| Cell (°N, °E) | % of district | Zone 1 | Zone 2 | Zone 3 | Zone 4 | Majority |
|---|---|---|---|---|---|---|
| 31.25, 77.00 | 4.8 | 1.2 | 80.3 | 17.1 | 1.4 | zone-2 |
| 31.25, 77.25 | 7.0 | 0.3 | 81.2 | 17.1 | 1.3 | zone-2 |
| 31.50, 76.75 | 6.2 | 4.7 | 95.3 | 0.1 | 0.0 | zone-2 |
| 31.50, 77.00 | 16.6 | 0.4 | 65.5 | 21.0 | 13.2 | zone-2 |
| 31.50, 77.25 | 13.7 | 0.0 | 19.6 | 27.1 | 53.3 | zone-4 |
| 31.75, 76.75 | 14.0 | 2.9 | 95.4 | 1.7 | 0.0 | zone-2 |
| 31.75, 77.00 | 15.9 | 0.0 | 84.2 | 9.5 | 6.3 | zone-2 |
| 31.75, 77.25 | 6.5 | 0.0 | 44.5 | 22.6 | 32.9 | zone-2 |
| 32.00, 76.75 | 9.0 | 1.2 | 80.4 | 10.6 | 7.7 | zone-2 |
| 32.00, 77.00 | 5.9 | 0.0 | 27.8 | 26.2 | 46.0 | zone-4 |
| **District** | 100.0 | 1.0 | 67.9 | 15.0 | 16.2 | |

Verdict bands if independent gauges existed: district (fewer-than-two-bands).

## What the tables show

- **One verdict band under every scheme.** In each, all 10 larger cells hold district area in at least two candidate zones, so the zones share cells and merge (R17). This holds even if independent gauges existed.
- **The majority label is not stable.** Under the Department of Agriculture's edges the larger cells are labelled zone 1, 2 or 3. Under the attributed CSK HPKV edges eight are zone 2 and two are zone 4. Only two cells keep their label.
- **The 2500 / 3250 m choice** moves 8.5% of the district between zone 3 and zone 4 and changes no majority label.
- **District area above the candidate bands** is 8.8%, 0.3% or 16.2%, depending on the scheme.

## Carried forward

- Cell weights use the whole district polygon (KTD4), including the area above the candidate bands, where no rainfed wheat grows. This is the one place elevation could affect a score. Changing it would mean a new band map file and a plan revision.
- No public list of gauges per IMD cell was found (U1). Without one, bands cannot count as independent.
- A primary source for CSK HPKV's zone edges is still wanted before any zone scheme is registered.

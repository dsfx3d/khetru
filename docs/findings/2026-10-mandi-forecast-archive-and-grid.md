# Mandi forecast archive and grid: U1 feasibility finding

- **Date:** 2026-10-10
- **Status:** Draft for the owner's verdict. The checks below were run; the verdict is the owner's.
- **Plan unit:** U1 in `docs/plans/2026-10-08-1554-feat-mandi-wheat-claim-ledger-plan.md`

## Proposed verdict

A hindcast can run. The TIGGE archive on the ECMWF Data Store is reachable with a new account and returns ECMWF ensemble precipitation for Mandi on the same 0.25° lattice the ledger already uses. One district-wide verdict band is the expected outcome.

| Question | Answer |
|---|---|
| Archive | TIGGE, ECMWF fields only, through the ECMWF Data Store (`tigge-forecasts`) |
| Usable seasons | At most 20, rabi 2006 to 2025. 2006 is unconfirmed (see Open items). 2026 is excluded by the plan |
| Licence | CC BY 4.0 for ECMWF fields, plus the ECMWF Terms of Use |
| IMD observations | Final and real-time 0.25° products both load via `imdlib`, on the same lattice |
| Cells | 14 cells touch the district; 10 of them hold 1% or more of its area |
| Default verdict bands | 1, district-wide (to be confirmed by U3) |
| Can a hindcast run? | Yes |

No pre-2026 October or November forecast value was requested or opened. Every forecast retrieval below is for 15 July.

## 1. Forecast archive: TIGGE

Access was opened on 2026-10-10 with a newly registered ECMWF account, after accepting the Data Store terms and the TIGGE licence. TIGGE is not closed to new users, so the GEFS fallback was not checked.

Three retrievals ran through the repo's own adapter (`fetch_forecasts.tigge_requests`), cropped to the Mandi box plus one cell of margin:

| Init | Requested | Result |
|---|---|---|
| 2025-07-15 00 UTC | control and 50 perturbed members | 61 + 3050 messages, about 1 MB, about 3 minutes |
| 2016-07-15 00 UTC | control only | 61 messages |
| 2007-07-15 00 UTC | control only | 61 messages |

What the files hold, the same in all three years:

- **Members:** control (`cf`, number 0) and 50 perturbed (`pf`, numbers 1 to 50). The GRIB reports an ensemble size of 51.
- **Steps:** 0 to 360 h every 6 h, 61 steps, none missing. This is coarser than the open-data runs, which are 3-hourly to 144 h.
- **Variable and units:** total precipitation, `paramId` 228228, in kg m⁻², cumulative from step 0. The adapter converts to mm.
- **Grid:** the Data Store interpolates to the requested 0.25° regular grid. The 6 × 7 cells returned are 31.0 to 32.25°N and 76.25 to 77.75°E, identical to the saved open-data records.
- **Adapter:** the 2025 run built a valid normalised record (61 steps, 50 perturbed members, mm) with no code change.

Two facts that later units need:

- **Model cycle is not in the file.** `generatingProcessIdentifier` is 255 in TIGGE GRIB, so the record's `model_cycle` comes out as `ecmwf-gpi-255` for every year. The plan wants the bundle to record model cycles and the report to flag changes. That will need a date-to-cycle table from ECMWF's published cycle history, not the GRIB header.
- **Delay.** The licence gives access 48 hours after the forecast's initial time. This matters only for the cross-source test, not for live claims, which read open data.

Licence, from the TIGGE licence page: ECMWF, DWD, ECCC, KMA, NCEP and UKMO fields are CC BY 4.0. Others, including IMD's own TIGGE forecasts, are CC BY-NC 4.0. The ledger uses ECMWF fields only. Works based on TIGGE must acknowledge TIGGE and credit the provider.

Checksums of the 2025 test files (kept outside the repo):

- control: `4ae747e93c40a7e60fcbaefcef32f3af743c754828766d164fae217caaa160ad`
- perturbed: `3637f9a16e26d8d6050c62445b377ce5aaf9e180f0584ee9dd25ffdddbaef5be`

## 2. Observations: IMD gridded rainfall

Checked with `imdlib` 0.3.1:

- **Final product:** yearly files for 2024 and 2025 download and load. The product page lists data from 1901.
- **Real-time product:** daily files load for 2026-07-01 to 03 and 2026-10-05 to 10. The file for 10 October was already available at 10:39 UTC that day.
- **Grid:** 135 × 129 points, 6.5 to 38.5°N and 66.5 to 100.0°E at 0.25°, in mm per day, with −999 as the missing value.
- **Lattice match:** the Mandi box selects the same 6 latitudes and 7 longitudes as the forecast records, so no regridding is needed.
- **Coverage:** no missing cells in the box on any day checked.

## 3. Cells against the district polygon

Polygon: geoBoundaries gbOpen, India ADM2 (2021 boundaries, ODbL 1.0, release `9469f09`, file SHA-256 `8bef6929fd65432e7dc775e7c44473e84efff064ebddbfd6b834e6291546db40`), feature "Mandi". Its area computes to 3954 km², against the official 3950 km².

Share of the district's area in each cell, in percent. Rows are cell-centre latitudes, columns are cell-centre longitudes:

| °N \ °E | 76.50 | 76.75 | 77.00 | 77.25 | 77.50 |
|---|---|---|---|---|---|
| 32.00 | 0 | 9.0 | 5.9 | 0 | 0 |
| 31.75 | <0.1 | 14.0 | 15.9 | 6.5 | 0 |
| 31.50 | 0 | 6.2 | 16.6 | 13.7 | 0.1 |
| 31.25 | 0 | <0.1 | 4.8 | 7.0 | 0.1 |

- 14 cells touch the district. 10 hold 1% or more of its area, and together those 10 hold over 99% of it.
- 5 cells lie at least half inside the district. Only one (31.5°N, 77.0°E) lies fully inside.
- The saved 6 × 7 box covers the whole district with at least one cell of margin, so `MANDI_BOX` does not need widening.

## 4. Can the bands be separated?

Probably not, and the plan expects this. Ten cells of roughly 28 km × 24 km each carry the district, and Mandi's terrain runs from valley floor to high ridge inside a single cell. A cell value is one number for all of it.

This check did not load a DEM, so it gives no measured band split per cell. That is U3's work. The proposed default is therefore the plan's KTD4 default: one district-wide verdict band, unless U3 finds that the agro-climatic zones fall in separate cells.

## Open items

| Item | State |
|---|---|
| IMD daily accumulation convention | Not confirmed from a source read during this check. The commonly cited convention is 24 hours ending 08:30 IST (03:00 UTC) on the stated date. The IMD product page does not state it. Confirm from Pai et al. (2014) before U4 fixes the window alignment |
| IMD final-release timing | Not found. The product page listed data to 2024 while the 2025 file already downloads, so the page lags the files |
| Gauges per cell | No public list found in one search. Without it there is no independent evidence for separating bands |
| First usable season | The dataset page says TIGGE has been available since October 2006. Whether ECMWF runs cover the whole 15 October to 15 November 2006 window was not checked, because that would mean requesting protected dates |
| Format across eras | July 2007, 2016 and 2025 share one layout. Other eras between them were not sampled; the plan's per-era decode test in U5 covers that |

## Owner decision

1. Accept or reject the proposed verdict: a hindcast can run on TIGGE ECMWF fields, with one district-wide verdict band as the default.
2. If accepted, U3 (band map) and the cross-source test in U5 are unblocked.

## Sources

- TIGGE dataset: https://ecds.ecmwf.int/datasets/tigge-forecasts
- TIGGE licence: https://ewds.climate.copernicus.eu/licences/tigge-licence
- Data Store API setup: https://ecds.ecmwf.int/how-to-api
- IMD 0.25° gridded rainfall: https://www.imdpune.gov.in/cmpg/Griddata/Rainfall_25_Bin.html
- Pai et al. (2014), MAUSAM 65(1), 1–18: https://mausamjournal.imd.gov.in/index.php/MAUSAM/article/view/851
- geoBoundaries India ADM2: https://www.geoboundaries.org/api/current/gbOpen/IND/ADM2/

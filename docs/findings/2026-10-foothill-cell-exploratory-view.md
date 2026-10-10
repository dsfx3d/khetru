# Foothill cell: an exploratory view beside the district verdict

- **Date:** 2026-10-11
- **Status:** Owner approval to build recorded on 2026-10-11 (see Decision). The registered area is not decided here.
- **Plan unit:** KTD4, KTD6 and U6 in `docs/plans/2026-10-08-1554-feat-mandi-wheat-claim-ledger-plan.md`

## Finding

Mandi district stays the registered verdict. One IMD 0.25° cell, centred 32.00°N, 76.75°E, is issued and scored beside it as an exploratory view, under the band name `view-foothill-cell`.

The view uses the same bundle values, claim rule, scoring rule and band-mean function as the district. Only the cell weights differ: the district is 14 cells weighted by district area, and the view is this one cell with weight 1. Its claims and scores are always `exploratory`. They enter no verdict, no skill figure, no hindcast and no band switch (R17), before or after the bundle v1 tag.

## Decision

Approved by the owner on 2026-10-11, after a council review of that date:

- `ledger/mandi-wheat/bands/band-map.csv` and its verdict band `district` are unchanged.
- The view is a new write-once file, `ledger/mandi-wheat/bands/view-foothill-cell.csv`. `view-foothill-cell.PROVENANCE.md` beside it says how it was made.
- The view stands for the whole cell, not only the part of it inside Mandi. One cell has one IMD value and one forecast value, so no boundary enters any number.
- **Which area is registered is the owner's decision at the bundle v1 tag** (about August 2027). Until then the district is the registered area and the view is only a view.

The council was split: 2 for this arrangement, 1 for registering the cell alone, 2 for a foothill belt of several cells.

## Evidence permitted for the later choice (2026-10-11)

The choice of registered area at the tag may rest only on:

- observations,
- gauge provenance,
- geography,
- agronomic guidance.

No forecast score, skill figure or claim outcome may be cited for it. This includes the rabi 2026 exploratory claims of either area, the district's and the view's.

Until bundle v1 is tagged, KTD11 applies to the view as it does to all rabi 2026 data: no Brier score or skill figure is computed, printed or stored for it. `evidence score` prints outcome labels and counts only, and counts the view on its own line.

## What had been seen when the view was added

- `ledger/mandi-wheat/base-rates.md`: the district's held windows and episode counts for 1991 to 2025.
- `ledger/mandi-wheat/power.md`: the power table for the district, run on 2026-10-10.
- The comparison of the cell with the district in the next section, from IMD observations only.
- No forecast for any past season, and no Brier score or skill for rabi 2026 (KTD11).

## Why this cell

Geography and guidance only.

- The cell is the Dhauladhar foothill corner of the district. It runs from 31.875°N to 32.125°N and 76.625°E to 76.875°E. Jogindernagar lies in it. Baijnath, across the district line, probably does too; this was not checked against a pinned boundary.
- 356.850 km² of the cell's 654.9 km² lie inside Mandi district, 54%. The cell holds 9.0% of the district's area.
- It is wetter than the district as a whole, and its rain in the sowing weeks does not always move with the district's (table below).
- The mid-hills guidance this project uses for the threshold is CSK HPKV's: 10 mm of rain in a week is enough for germination (Prasad, Rao & Rao 2015, section 3.10, https://zenodo.org/records/14292957, as cited in the council review). It most likely comes from Palampur, in the same foothill belt west of this cell. The source was not re-read for this finding.

## The cell against the district, IMD observations

From the committed IMD final observations, vintage `final-r20261010`, seasons 1991 to 2025 (35). District values are `obs_core.band_daily` on `band-map.csv`; cell values are the same function on `view-foothill-cell.csv`. Windows are the 7 IMD dates of each issue date under the development values in `bundles/dev/`: 159 windows, none unverifiable in either area. No forecast enters this table.

| Quantity | Cell 32.00°N, 76.75°E | District band |
|---|---|---|
| Mean annual rain | 1,833 mm | 1,207 mm |
| Mean rain, 15 Oct to 15 Nov | 22.7 mm | 18.5 mm |
| Windows reaching 10 mm in 7 days | 21 of 159 (13.2%) | 18 of 159 (11.3%) |

The two areas agree on held or not held in 152 of the 159 windows at 10 mm. Of the 7 where they differ, the cell held and the district did not in 5, and the reverse in 2.

`py/evidence/tests/test_views.py` recomputes these figures from the committed files.

## What the comparison shows

- **The cell is wetter over the year by about half**, and by about a fifth in the sowing weeks.
- **At 10 mm the event is about as rare in the cell as in the district.** Independent episodes were not counted for the cell, but the view is unlikely to ease the shortage of rain episodes recorded in the plan's U8 blocker.
- **The district band mostly tells the same story as the cell**, but not always: 7 windows in 35 seasons differ.

## Carried forward

- One IMD cell is an interpolated value. No public list of gauges per cell was found (U1), so which gauges stand behind this cell is unknown. Gauge provenance is one of the permitted grounds for the later choice and is still missing.
- The cell is only about half inside Mandi. A view of the whole cell therefore describes land in two districts.
- A foothill belt of several cells, the option two council members preferred, would be another new file under `bands/`. Nothing here builds it.
- After the tag, the Monday workflow (U11) must score `claims/exploratory.jsonl` as well as `claims/live.jsonl`, or the view's claims go unscored.

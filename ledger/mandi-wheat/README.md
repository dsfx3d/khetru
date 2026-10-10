# Mandi wheat claim ledger

Append-only record of khetru's sow-or-wait rain claims for rainfed wheat in
Mandi district. `uv run --all-packages evidence verify --base <ref>` checks
that the ledger only grew since `<ref>`; CI runs it on every push and PR
(`.github/workflows/evidence-verify.yml`).

## Repository rules (owner action)

The owner sets up a GitHub ruleset on `github.com/dsfx3d/khetru`
(Settings → Rules → Rulesets); it cannot be set from code:

- Branch ruleset targeting `main`: block force pushes and block deletion.
- Tag ruleset targeting `mandi-wheat/*`: restrict updates and restrict
  deletions, so a bundle tag can never be moved or removed (R14).

`verify` also fails when a tag recorded in `tags.jsonl` no longer resolves to
its recorded commit, so a moved tag is caught even without the ruleset.

## Forecast inputs

`inputs/ens-<source>-<YYYYMMDDHH>.json.gz` holds one ECMWF ENS run each
(`source` is `opendata` or `tigge`): gzip-compressed canonical JSON with the
control and every perturbed member kept apart, cumulative precipitation in mm at
every native step from 0 to 360 h, on IMD 0.25° lattice cells covering the
Mandi box plus one cell of margin, and the run's init time, model cycle, source
URL and raw-GRIB SHA-256. Window totals are not stored; rebuild them with
`khetru_evidence.forecasts.window_totals`. Files are write-once: `verify`
fails if a committed one changes. The daily `evidence-archive` workflow saves
the 00 and 12 UTC open-data runs from 1 Oct to 30 Nov.

## Band map

`bands/band-map.csv` lists every IMD 0.25° lattice cell holding part of Mandi
district: the cell's share of the district's area (`weight`) and the verdict
band it reports to. There is one verdict band, `district`, because no
independent gauges stand behind separate bands (R17). Elevation zones are not
registered; `docs/findings/2026-10-mandi-elevation-zones.md` says why. `khetru_evidence.bands.band_mean` turns a forecast or
observed field into the band value with these weights and one coverage rule.
`bands/PROVENANCE.md` names the sources, their hashes and the method;
`uv run --all-packages evidence bands build` (needs `uv sync --all-extras`)
rebuilds the map and fails unless it matches the committed file byte for byte.

## Observations

`observations/imd-<vintage>-<first>-<last>.json.gz` holds IMD 0.25° gridded
daily rainfall for a run of consecutive dates: gzip-compressed canonical JSON
with mm per day on the same cells as the forecast inputs (`null` where IMD has
no value), the source URL, and the SHA-256 of each raw IMD file. A vintage is
`final-r<YYYYMMDD>` or `realtime-r<YYYYMMDD>`, after the UTC date of retrieval.
Files are write-once: a later retrieval of the same dates is a new vintage in a
new file, and both stay. `khetru_evidence.observations.load` reads one vintage
by name.

IMD's date D is the 24 hours ending 08:30 IST (03:00 UTC) on D. IMD's bulletins
state this for station rainfall ("rainfall for the 24 hrs ending at 0830 hrs of
date"); Pai et al. (2014), the paper behind the gridded product, does not state
it, so the grid is taken to keep its stations' convention. A claim window's
rain-day ending 03:00 UTC on D is therefore IMD's date D.

- `uv run --all-packages evidence observe fetch-final --years 1991 2025` saves
  one record per year from IMD's yearly files (needs `uv sync --all-extras`). A
  year whose raw file is already saved is skipped.
- `uv run --all-packages evidence observe fetch-realtime --dates 2026-10-01 2026-10-09`
  saves one record of IMD's provisional daily files.
- `uv run --all-packages evidence observe base-rates --vintage <final vintage> [--provisional <real-time vintage>]`
  writes `base-rates.md`; with `--check` it fails unless the committed table
  matches. The tests run the same check.

`base-rates.md` uses the development rule values in
`bundles/dev/observations.toml`. They are placeholders; the owner chooses the
bundle v1 values at U8.

## Claims

`claims/<kind>.jsonl` holds one entry per verdict band and issue date: a claim,
an abstain, or `not_issued`. A claim states a probability that the band's mean
rain over the claim window reaches the bundle's threshold: k of the run's n
members reach it, and the bundle's formula turns k and n into the probability.
Each entry names the bundle files, band map and forecast record it was made
from by SHA-256 under `evidence`, and the commit of the code under `code`.

- `uv run --all-packages evidence issue --date 2026-10-19` appends that issue
  date's claim per verdict band from the saved 00 UTC run. Issuing a date again
  writes nothing. With no saved run it fails; `--abstain-if-missing` records
  "can't tell — no forecast coverage for this band" instead.
- `uv run --all-packages evidence issue --backfill-missing` records `not_issued`
  for every issue date whose window has started with no entry. It never writes
  a claim, and a slot marked `not_issued` stays that way.
- `uv run --all-packages evidence issue --check` re-derives every claim from
  the files it names and fails if one differs.

Entries under the `dev` bundle are `exploratory`. Its claim rule values in
`bundles/dev/claims.toml` are placeholders; the owner chooses the bundle v1
values at U8. Live entries are written by the GitHub workflows only.

## Scores

`scores/<kind>.jsonl` holds each claim's score against one observation vintage:
the band's rain on each IMD date of the window, the outcome (`held`,
`not_held`, or `unverifiable` when a date has no band value), the probability
that is scored, and climatology's probability for the same window, from the
final vintage the bundle names with the claim's own season left out. An
abstain, a `not_issued` slot, a voided claim and a late claim are scored as if
they had stated climatology's probability. Brier scores are not stored; they
follow from the outcome and the two probabilities. Each score names the files
it was made from by SHA-256 under `evidence`.

- `uv run --all-packages evidence score --vintage realtime-r20261028` appends a
  score for every claim whose window has closed and whose dates the vintage
  holds. Scoring a claim and vintage again writes nothing. A later vintage
  appends a score that supersedes the current one and states what changed; the
  earlier score stays. Real-time observations never replace final ones.
- `uv run --all-packages evidence score --check` re-derives every score from
  the files it names and fails if one differs.

The command prints outcomes and counts only. Skill against climatology is read
through `khetru_evidence.scoring.standing`, which refuses a bundle with no tag
recorded in `tags.jsonl`, so rabi 2026 development data never shows it. The
pass/fail rule values in `bundles/dev/scoring.toml` are placeholders; the owner
chooses the bundle v1 values at U8. A live claim's score waits for the
OpenTimestamps proof of when it was made; there is no stamping yet (U8), so
live claims are not scored. Exploratory and hindcast claims are timed by when
their forecast run was available.

## Data attribution

Observed rainfall: India Meteorological Department, 0.25° gridded daily
rainfall (https://www.imdpune.gov.in/cmpg/Griddata/Rainfall_25_Bin.html) and
its real-time counterpart. IMD asks that work using it cite Pai D.S., Latha
Sridhar, Rajeevan M., Sreejith O.P., Satbhai N.S. and Mukhopadhyay B. (2014),
MAUSAM 65(1), 1-18. The files here are cropped to the Mandi box and rounded to
0.001 mm. IMD's download pages state no licence.


Forecast data: ECMWF, licensed under Creative Commons Attribution 4.0
International (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/.
Open-data runs come from ECMWF open data (https://www.ecmwf.int/en/forecasts/datasets/open-data);
hindcast runs from the TIGGE archive via the ECMWF Data Store, ECMWF fields only
(CC BY 4.0). The files here are cropped and converted (metres or kg m-2 to mm,
rounded to 0.001 mm); ECMWF does not endorse this use.

District boundary: geoBoundaries gbOpen India ADM2 (https://www.geoboundaries.org),
Open Database License 1.0. Elevation: Copernicus DEM GLO-30, produced using
Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH
2014-2018, provided under COPERNICUS by the European Union and ESA; all rights
reserved.

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

## Development bundles

A bundle is a directory of rule values under `bundles/`. A development bundle
is untagged: every entry made under it is `exploratory`, and the ledger refuses
any other kind. A bundle is a development bundle when its name is `dev` or
starts with `dev-`. Under any other name `evidence issue` writes `live`
entries, on the GitHub workflows only, and only when the bundle's tag is
recorded in `tags.jsonl`. A name that is neither, such as `dev2` or `dev_2`,
is refused: "bundle dev2 is not a development bundle (dev, dev-*) and has no
tag recorded in tags.jsonl". `evidence score --kind live` refuses the same way
when a claim in `claims/live.jsonl` names an untagged bundle.

There are two, because files under `bundles/` are write-once:

- `bundles/dev/` is the first. Its issue dates are the Mondays from 15 Oct to
  15 Nov and its sowing cutoff is 30 Nov. It is not edited. `base-rates.md`,
  `power.md` and `power-december.md` are made from it and still reproduce.
- `bundles/dev-2/` is the current one. On 2026-10-11 the owner decided that
  issue dates run to 7 Dec, so its issue dates are the Mondays from 15 Oct to
  7 Dec: in 2026, 19 and 26 Oct, 2, 9, 16, 23 and 30 Nov, and 7 Dec. Its sowing
  cutoff is 16 Dec, the end of the last sowing week in the CSK HPKV guidance
  this project uses; an agent chose that date and the owner accepted it on
  2026-10-11, as a starting value for bundle v1. Every other value is `dev`'s.

`evidence issue` uses `dev-2` unless `--bundle` names another, so the weekly
commands below issue the whole rabi 2026 season under `dev-2`. `--bundle dev`
still works for the dates `dev` holds. A band and issue date are held under
one development bundle only, so no date is scored twice: `evidence issue
--date` fails when another development bundle already holds the date ("already
has a district slot under development bundle dev"), and `--backfill-missing`
writes nothing for such a date. `evidence score` takes no bundle: it scores
each claim by the bundle the claim names.

Both hold development values. The owner chooses the bundle v1 values at U8.

## Forecast inputs

`inputs/ens-<source>-<YYYYMMDDHH>.json.gz` holds one ECMWF ENS run each
(`source` is `opendata` or `tigge`): gzip-compressed canonical JSON with the
control and every perturbed member kept apart, cumulative precipitation in mm at
every native step from 0 to 360 h, on IMD 0.25° lattice cells covering the
Mandi box plus one cell of margin, and the run's init time, model cycle, source
URL and raw-GRIB SHA-256. Window totals are not stored; rebuild them with
`khetru_evidence.forecasts.window_totals`. Files are write-once: `verify`
fails if a committed one changes. The daily `evidence-archive` workflow saves
the 00 and 12 UTC open-data runs from 1 Oct to 16 Dec.

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

## Exploratory views

`bands/view-<name>.csv` is an area issued and scored beside the verdict bands,
by the same rules and the same band-mean function, with its own cell weights.
It is not a verdict band. Its band is named `view-<name>`, and its claims and
scores are always `exploratory`, under any bundle and after any tag: the
ledger refuses them under `live` or `hindcast`, and
`khetru_evidence.scoring.standing` leaves them out, so a view never adds to a
verdict, a skill figure or a band switch. `evidence issue` and `evidence score`
handle a view with the verdict bands, in `claims/exploratory.jsonl` and
`scores/exploratory.jsonl`, and `evidence score` counts it on its own line.
Each claim and score names its own area's file under `evidence`, never
another's.

There is one view, `bands/view-foothill-cell.csv`: the single IMD cell centred
32.00°N, 76.75°E, the Dhauladhar foothill corner of the district, with weight 1.
It stands for the whole cell. `bands/view-foothill-cell.PROVENANCE.md` says how
the file was made. `docs/findings/2026-10-foothill-cell-exploratory-view.md`
says why it exists, and that the later choice of registered area may cite
observations, gauge provenance, geography and agronomic guidance only, never a
forecast score, skill figure or claim outcome of either area.

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
- The same command with `--bundle dev-2` writes or checks
  `base-rates-dev-2.md`.

`base-rates.md` uses the development rule values in `bundles/dev/`: issue
dates to 15 Nov, sowing cutoff 30 Nov. `base-rates-dev-2.md` uses those in
`bundles/dev-2/`: issue dates to 7 Dec, sowing cutoff 16 Dec, and missing cell
values counted through December. Both read the IMD observations only. The
values are placeholders; the owner chooses the bundle v1 values at U8.

## Claims

`claims/<kind>.jsonl` holds one entry per verdict band and issue date: a claim,
an abstain, or `not_issued`. A claim states a probability that the band's mean
rain over the claim window reaches the bundle's threshold: k of the run's n
members reach it, and the bundle's formula turns k and n into the probability.
Each entry names the bundle files, band map and forecast record it was made
from by SHA-256 under `evidence`, and the commit of the code under `code`.

- `uv run --all-packages evidence issue --date 2026-10-19` appends that issue
  date's claim per verdict band from the saved 00 UTC run, under `dev-2`.
  Issuing a date again writes nothing. With no saved run it fails;
  `--abstain-if-missing` records "can't tell — no forecast coverage for this
  band" instead.
- `uv run --all-packages evidence issue --backfill-missing` records `not_issued`
  for every issue date whose window has started with no entry. It never writes
  a claim, and a slot marked `not_issued` stays that way.
- `uv run --all-packages evidence issue --check` re-derives every claim from
  the files it names and fails if one differs.

Entries under a development bundle (`dev`, `dev-2`) are `exploratory`. The
claim rule values in `bundles/dev-2/claims.toml`, the same as in
`bundles/dev/claims.toml`, are placeholders; the owner chooses the bundle v1
values at U8. Live entries are written by the GitHub workflows only.

## Scores

`scores/<kind>.jsonl` holds each claim's score against one observation vintage:
the band's rain on each IMD date of the window, the outcome (`held`,
`not_held`, or `unverifiable` when a date has no band value), the probability
that is scored, and climatology's probability for the same window, from the
final vintage the bundle names with the claim's own season left out. A season
of that vintage counts only when it holds every date of the months the
bundle's windows and sowing cutoff use (October to December for `dev-2`), and
a score is refused when no season does. An
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
through `khetru_evidence.scoring.standing`, which refuses a development bundle
whatever tag is recorded, and any bundle with no tag recorded in `tags.jsonl`,
so rabi 2026 development data never shows it. The
pass/fail rule values in `bundles/dev/scoring.toml` and
`bundles/dev-2/scoring.toml` are the same placeholders; the owner
chooses the bundle v1 values at U8. A live claim's score waits for the
OpenTimestamps proof of when it was made; there is no stamping yet (U8), so
live claims are not scored. Exploratory and hindcast claims are timed by when
their forecast run was available.

## Weekly job

`.github/workflows/evidence-weekly.yml` runs the commands above on a schedule,
from October to December, so no issue date depends on someone remembering it.
In rabi 2026 it is a rehearsal: it issues under `dev-2`, so every entry is
`exploratory`.

- Mondays at 10:30 UTC, and again at 12:30 and 14:30: it saves that day's
  00 UTC run, from ECMWF and then from the AWS mirror, runs `evidence issue
  --date <today> --skip-other-dates`, and then `--backfill-missing`. A run that
  finds no forecast fails and the next one tries again. The 14:30 run passes
  `--abstain-if-missing`, so a Monday with no forecast by then is an abstain.
  On a Monday that is not an issue date it writes nothing.
- Tuesday to Thursday at 12:00 UTC: `evidence score --due` prints the first and
  last IMD date of the closed claims that have no score. The job fetches those
  dates as one real-time record and scores against it. A claim is scored once
  this way, so a later week's retrieval never scores an earlier week again. If
  IMD has not published a date yet the job fails, and the next day tries again.

Each run checks `evidence verify`, `evidence issue --check` and `evidence score
--check` before it commits, and pushes to `main` in the same single-writer group
as the archive job. Its summary on GitHub shows what the commands printed:
outcomes and counts, never a Brier score or a skill figure. A run can also be
started by hand from the Actions tab, as `issue` or `score`.

Not in it yet: OpenTimestamps stamping (U8), the report (U10), and scores on
IMD final data, which are still made by hand with `evidence score --vintage`.

## Power simulation

`uv run --all-packages evidence bundle power` writes `power.md`: for each
candidate event and threshold, the rain episodes in each hindcast season range,
the live seasons until a band can be judged, how often a hindcast of a given
true skill would pass, and how often the live check would fail or pass. It
reads the IMD final observations and no forecast; forecasts are simulated at a
stated skill, with a season's errors independent and again shared. With `--check` it fails unless the committed table matches. The
procedure is `docs/findings/2026-10-mandi-power-simulation-procedure.md`; it
was committed before the first run, and a later change to it is listed there.
A run takes about 15 minutes, so no test repeats it. The owner chooses the bundle v1 values from the table at
U8.

`evidence bundle power --variant december` writes `power-december.md`: a
recount made on 2026-10-11, after the first run, with issue dates on every
Monday to 14 Dec and a sowing cutoff of 16 Dec, for the district and then the
exploratory view. It is evidence for the owner's choice of issue dates; the
values are not adopted, no bundle holds them, and `power.md` is unchanged.

`evidence bundle power --variant december-7` writes `power-december-7.md`: a
second recount made on 2026-10-11, with issue dates on every Monday to 7 Dec
and a sowing cutoff of 16 Dec, for the district and then the exploratory view.
These are the issue dates the owner chose after reading `power-december.md`,
as the starting value for bundle v1; the owner freezes the values at the tag.
The table's header calls the 16 Dec cutoff the agent's choice and open for the
owner: it was when the table was made. The owner accepted it later that day,
as a starting value too, and the table is not written again for that. The
simulation changes the two values in code and reads every other value from
`bundles/dev`, which holds neither. The development bundle `bundles/dev-2`
holds the same two values, for issuing rabi 2026 claims; the simulation does
not read it. `power.md` and `power-december.md` are unchanged.

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

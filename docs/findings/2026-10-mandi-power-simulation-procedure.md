# Mandi power simulation: the procedure, written before the run

- **Date:** 2026-10-11
- **Status:** Accepted by the owner with the merge of PR #10 on 2026-10-10. First run on the IMD files on 2026-10-10 from commit `2ebd930`; its output is `ledger/mandi-wheat/power.md`.
- **Plan unit:** U8 step 2 in `docs/plans/2026-10-08-1554-feat-mandi-wheat-claim-ledger-plan.md`

## Purpose

The owner chooses the bundle v1 values at U8: the event and threshold, the hindcast season range, the minimum N, the confidence level and the live-check margin. The simulation shows, for each candidate, what the pass/fail rule could and could not decide. The plan asks that this procedure is committed before the simulation is run, so that no candidate or method is chosen after seeing a result.

The code is `py/evidence/src/khetru_evidence/bundle.py`, run by `uv run --all-packages evidence bundle power`. It writes `ledger/mandi-wheat/power.md`. The candidate values below are the defaults of `bundle.Design`; the two must match.

## What had been seen when this was written

- `ledger/mandi-wheat/base-rates.md` (U4): the held windows and episode counts for 1991–2025. The blocker in the plan (17 positive episodes at 10 mm in 35 seasons) comes from it.
- No forecast for any past season, and no Brier score or skill for rabi 2026 (KTD11).
- No output of this simulation on the IMD files. The code was tested on synthetic rainfall only (`py/evidence/tests/test_bundle.py`).

## Inputs

- IMD final observations of the vintage named in `bundles/dev/scoring.toml` (`final-r20261010`), seasons 1991 to 2025.
- `bands/band-map.csv`: one verdict band, `district`.
- The development rule values in `bundles/dev/`: issue dates, the 7-day window, coverage share 1, wet day 1 mm, dry-day gap 3, climatology ±7 days with pseudo-count 0.5, sowing cutoff 30 Nov, pass when the lower skill bound is above 0, seed 20261008.
- No forecast. The simulation reads the observations that will also decide the outcomes, as the plan allows.

## Candidates

| What | Values |
|---|---|
| Event | rain in the 7-day window; next rain before the sowing cutoff |
| Threshold | 2, 5, 10, 20 mm |
| Hindcast season range, counted | 2006–2025, 2007–2025, 2016–2025 |
| Hindcast season range, simulated | 2006–2025, 2016–2025 |
| Minimum N | 4, 6, 8, 10, 12 |
| Confidence level (two-sided) | 0.8, 0.9, 0.95 |
| Live-check margin | 0.05, 0.1, 0.2 |
| True skill (BSS against climatology) | 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5 |
| Forecast errors within a season | independent; shared |

2006 is counted with and without, because U1 could not confirm that TIGGE covers its whole window. The threshold over climatology stays at the development value of 0; it is not varied.

## Events and episodes

- **Rain in the 7-day window.** As in U4: the window's band rain reaches the threshold. Episodes are `obs_core.episode_counts`: held windows fed by the same rain episode are one positive episode, and a run of windows that did not hold is one negative episode.
- **Next rain before the sowing cutoff.** As in U4: some 7 dates in a row, from the window's first date to the cutoff, reach the threshold. If a later issue date of a season holds, every earlier one holds on the same rain. So its episodes are the runs of held and of not-held issue dates in a season: at most one positive and one negative per season. This episode rule is new here and is not in `obs_core`.
- Climatology for both events leaves the scored season out and pools the starts within ±7 days over the other 34 seasons, with the pseudo-count. The cutoff event uses `bundle.cutoff_climatology`, the same pooling as `obs_core.climatology`.
- An issue date with a missing value is left out. U4 found none.

## Steps

1. **Episodes in each season range.** For each event, threshold and counted range: issue dates, held dates, base rate, positive and negative episodes, and the largest candidate minimum N that both counts reach. This is a count, not a simulation. The hindcast's N is known from observations alone, so it shows at once which minimum N a hindcast range can meet.
2. **Live seasons until a band can be judged.** For each event, threshold and minimum N: draw seasons with replacement from the 35 observed seasons, adding their episode counts, until positive and negative episodes both reach N. Repeat 1000 times; report the median and the 90th percentile of the number of seasons. A row that does not get there in 200 seasons in 90% of draws reads "over 200".
3. **Simulated forecasts.** Outcomes are the observed ones. For a true skill `s`, each issue date's forecast is drawn from the Beta distribution a calibrated forecaster would have, given the outcome. With climatology `c` and `v = (1 − s) / s`: Beta(c·v + 1, (1 − c)·v) when the event held, Beta(c·v, (1 − c)·v + 1) when it did not. These forecasts are reliable, and their expected BSS against climatology is `s`. A season's draws are made in two ways, each with its own rows:
   - **independent:** each issue date has its own draw.
   - **shared:** one uniform draw per season says how good its forecasts are. Each issue date takes that quantile of its own Beta distribution, counted from the top when the event held and from the bottom when it did not. A season's forecasts are then all as good or as bad as one another, and each forecast keeps the distribution it has when drawn independently, so reliability and the expected BSS are unchanged. Seasons stay independent of one another.
4. **Hindcast pass rate and minimum detectable skill.** For each event, threshold, simulated range, true skill and kind of errors, make 1000 simulated hindcasts. Each is judged by `score_core.skill` (cluster bootstrap over seasons, 2000 resamples, the bundle seed) and `score_core.hindcast_verdict`, with N taken as met, at each confidence level. Report the share that pass. The minimum detectable skill is the smallest candidate skill that passes at least 80% of the time.
5. **Live check.** Each simulated hindcast is paired with one live season drawn from the 35 observed seasons, with the same kind of errors, and `score_core.skill_difference` and `score_core.live_check` are read at each confidence level and margin, for two live seasons:
   - one with the hindcast's true skill. Its fail rate is the false-fail rate; its pass rate is also reported.
   - one that states climatology (skill exactly 0). Its fail rate is how often the check catches a live season with no skill.

Every random draw comes from numpy's generator seeded with the bundle seed and the row's position (band, event, threshold, range, skill and kind of errors, or minimum N). The same files and seed give the same `power.md`; `evidence bundle power --check` exits 1 unless the committed table matches. The run takes about 15 minutes on 12 cores.

## Limits to read the output with

- **Real forecast errors lie between the two kinds simulated.** Errors in one season are likely to be related, most of all for the cutoff event, where one rain decides several issue dates. The independent rows are an upper bound on power and the shared rows a lower bound. Shared errors assume a season is good or bad as a whole; errors shared by only some of a season's issue dates are not simulated.
- **Simulated forecasts are perfectly reliable.** A real forecast with the same resolution but poor reliability scores lower.
- **The live season is one season drawn from the past 35.** A live check pooled over more seasons is not simulated.
- **The cutoff event looks up to 45 days ahead.** The ENS run reaches 15 days and the development claim rule covers steps 24–192 h. The simulation says how well a forecast of that event could be judged, not whether one can be made. Choosing it needs a new claim rule.
- **1000 simulated records per row.** A reported rate is within about 3 percentage points of its long-run value (95%).
- **The sowing cutoff of 30 Nov is unchecked** against the CSK HPKV package of practices (open since U4).

## Open for the owner before the run

1. Whether these candidates are the ones to simulate. Adding one after the run is a change to this procedure.
2. Whether the episode rule for the cutoff event (at most one positive and one negative per season) is the one to count with.

Decided by the owner on 2026-10-11, before the run: rows with errors shared within a season are added beside the independent ones (step 3).

## Changes after the run

Any change to this procedure or to `bundle.Design` after the first run on the IMD files is listed here with its date and reason. The earlier `power.md` stays in git history.

### 2026-10-11: recount with issue dates to 14 Dec

- **What was added.** A second table, `ledger/mandi-wheat/power-december.md`, written by `uv run --all-packages evidence bundle power --variant december`. It repeats every step above with two development values changed: issue dates on every Monday from 15 Oct to 14 Dec (the first run stops at 15 Nov), and a sowing cutoff of 16 Dec (the first run has 30 Nov). It counts two areas: the verdict band `district`, then the exploratory view `view-foothill-cell`, which is IMD cell 32.00N 76.75E alone (`bands/view-foothill-cell.csv`). A view is read beside the verdict bands and is never one of them. Every other value, and every default of `bundle.Design`, is the first run's.
- **Why.** The CSK HPKV guidance the owner follows gives the last wheat sowing week as 10–16 Dec, and the owner dropped 30 Nov as the cutoff. The owner asked for the recount before deciding whether issue dates should run to mid-December. The recount is evidence for that decision. The owner has not adopted December issue dates.
- **The first run is unchanged.** `power.md` is the same bytes, and `evidence bundle power --check` still reproduces it. The changed values are a named variant in `bundle.py` (`bundle.DECEMBER`), applied to the loaded development values. No bundle was written for them, because a bundle directory is permanent and `bundles/dev/` is not edited.
- **What had been seen when it was made.** `power.md`, `base-rates.md` and the CSK HPKV guidance. No forecast for any season, and no Brier score or skill for rabi 2026 (KTD11). The code was tested on synthetic rainfall before the run on the IMD files.
- **Seeds.** The first run's seeds are unchanged. The variant puts 2 and 3 where the first run has 0 and 1 in the second position of each seed, and the view takes band position 1. No row of either table shares a seed with another.
- **Seasons and missing values.** The earlier check covered October and November only. The recount needs every date from 10 Oct to 29 Dec at the widest. All 35 seasons have them. December has no missing cell value in either area, and no issue date is unverifiable. The table's header gives the counts.
- **Issue dates with no room before the cutoff.** "Next rain before the sowing cutoff" needs 7 dates in a row from the window's first date to the cutoff. With a cutoff of 16 Dec, a window that starts after 10 Dec has no room for them, and `obs_core.rain_before` would call it not held. Such an issue date is left out of that event and of that event's climatology, as an unverifiable one is. This leaves out 30 of 304 issue dates: the last one of each season, but for the 5 seasons where 8 Dec is a Monday (1997, 2003, 2008, 2014, 2025). The 7-day event keeps all 304. With the first run's values every issue date has room, so the first run is not affected.
- **The limit on the cutoff event grows.** With the later cutoff it looks 16 days further ahead than in the first run.
- **The agent's choices, open for the owner.** These three were made by the agent to run the recount, not by the owner:
  1. The cutoff of 16 Dec, taken as the last day of the guidance's last sowing week.
  2. The last issue date of 14 Dec. A window is the 7 dates from 2 days after its issue date, so a season's last window ends after the cutoff (on 17–22 Dec), but for the 5 seasons where 8 Dec is a Monday.
  3. Leaving out the issue dates with no room, where counting them as not held was the other choice.

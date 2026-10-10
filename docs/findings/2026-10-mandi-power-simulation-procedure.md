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

- None.

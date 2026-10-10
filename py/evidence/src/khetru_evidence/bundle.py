"""The pre-registration bundle (U8, unfrozen). So far: the power simulation.

The simulation says what the pass/fail rule can and cannot decide for each
candidate event and threshold, so the owner can choose the bundle v1 values. It
reads the saved IMD final observations and no forecast (KTD11). Its procedure
is `docs/findings/2026-10-mandi-power-simulation-procedure.md`; the candidate
values below are that procedure's, and changing one changes the procedure.

Outcomes are the observed ones. A forecast of true skill ``s`` is drawn for
each issue date from the Beta distribution a calibrated forecaster would have
given that outcome: with climatology ``c`` and ``v = (1 - s) / s``, a held
window draws from Beta(c v + 1, (1 - c) v) and one that did not hold from
Beta(c v, (1 - c) v + 1). Such forecasts are reliable and their expected Brier
skill score against climatology is ``s``. Every verdict is then read with the
``score_core`` functions the real record will use.
"""

import dataclasses
import functools
import hashlib
import math
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from khetru_evidence import bands, obs_core, observations, score_core, semantics

POWER_PATH = "power.md"
WINDOW, BEFORE_CUTOFF = "window", "before_cutoff"
EVENTS = (WINDOW, BEFORE_CUTOFF)


@dataclass(frozen=True)
class Design:
    """The candidate values and the size of the simulation."""

    thresholds_mm: tuple[float, ...] = observations.THRESHOLDS_MM
    # First season of each hindcast range that is counted; every range ends at the last final year.
    counted_first_years: tuple[int, ...] = (2006, 2007, 2016)
    # The ranges that are also simulated.
    simulated_first_years: tuple[int, ...] = (2006, 2016)
    min_episodes: tuple[int, ...] = (4, 6, 8, 10, 12)
    confidences: tuple[float, ...] = (0.8, 0.9, 0.95)
    margins: tuple[float, ...] = (0.05, 0.1, 0.2)
    skills: tuple[float, ...] = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5)
    power_target: float = 0.8  # the smallest skill passing this often is the minimum detectable one
    replicates: int = 1000
    bootstrap_resamples: int = 2000
    season_cap: int = 200  # live seasons drawn before a band is reported as not judged


@dataclass(frozen=True)
class Slot:
    """One issue date of one event: what was observed and what climatology said."""

    issue_date: str
    outcome: str
    climatology: float


@dataclass(frozen=True)
class Season:
    year: int
    slots: tuple[Slot, ...]  # issue dates with an outcome; unverifiable ones are left out
    positive: int  # independent episodes
    negative: int


@dataclass(frozen=True)
class Power:
    """What ``replicates`` simulated records gave for one event, threshold, range and true skill."""

    replicates: int
    passes: Mapping[float, int]  # confidence -> hindcasts whose lower skill bound passes
    # (confidence, margin) -> live-check verdicts when the live season has the hindcast's skill ...
    live_equal: Mapping[tuple[float, float], Counter]
    # ... and when the live season states climatology.
    live_none: Mapping[tuple[float, float], Counter]


# --- observed seasons ---------------------------------------------------------


def runs(outcomes: Sequence[str]) -> tuple[int, int]:
    """(runs of held, runs of not held) among ``outcomes`` in time order."""
    positive = negative = 0
    previous = None
    for result in outcomes:
        if result != previous:
            positive += result == obs_core.HELD
            negative += result == obs_core.NOT_HELD
        previous = result
    return positive, negative


def cutoff_climatology(outcome: Callable[[date], str], first: date, *, years, half_window_days: int,
                       pseudo_count: float) -> obs_core.Climatology:
    """``obs_core.climatology`` for "rain before the cutoff": pools the same starts in ``years``.

    ``outcome`` gives the event's outcome for a window starting on a date.
    """
    events = windows = 0
    for year in sorted(set(years)):
        centre = first.replace(year=year)
        for shift in range(-half_window_days, half_window_days + 1):
            result = outcome(centre + timedelta(days=shift))
            if result != obs_core.UNVERIFIABLE:
                windows += 1
                events += result == obs_core.HELD
    return obs_core.Climatology((events + pseudo_count) / (windows + 2 * pseudo_count), events, windows)


def rain_before_cutoff(band: Mapping[date, float], days: int, threshold_mm: float,
                       cutoff: tuple[int, int]) -> Callable[[date], str]:
    """The outcome of "rain before the cutoff" by the window's first date, worked out once per date."""
    @functools.cache
    def outcome(first: date) -> str:
        return obs_core.rain_before(band, first, date(first.year, *cutoff), days, threshold_mm)
    return outcome


def observed_seasons(band: Mapping[date, float], years: Sequence[int], sem: semantics.Semantics,
                     rules: obs_core.Rules, event: str, threshold_mm: float) -> list[Season]:
    """Each season's issue dates for ``event``, with leave-one-season-out climatology from ``years``.

    ``window`` is rain in the observed window, with ``obs_core``'s episodes.
    ``before_cutoff`` is some run of the window's length reaching the threshold
    between the window's first date and the sowing cutoff. A season's held issue
    dates then lean on the same rain, so its episodes are the runs of held and
    of not-held issue dates: at most one of each.
    """
    pooling = {"half_window_days": rules.climatology_half_window_days,
               "pseudo_count": rules.climatology_pseudo_count}
    before_cutoff = rain_before_cutoff(band, sem.rain_days, threshold_mm, rules.sowing_cutoff)
    out = []
    for year in years:
        others = [y for y in years if y != year]
        issues = semantics.issue_dates(sem, year)
        windows = [obs_core.imd_dates(semantics.schedule(sem, issue)) for issue in issues]
        if event == WINDOW:
            results = [obs_core.outcome(obs_core.window_total(band, w), threshold_mm) for w in windows]
            climatology = [obs_core.climatology(band, w[0], years=others, days=sem.rain_days,
                                                threshold_mm=threshold_mm, **pooling) for w in windows]
            positive, negative = obs_core.episode_counts(band, windows, threshold_mm, rules.wet_day_mm,
                                                         rules.dry_day_gap_days)
        elif event == BEFORE_CUTOFF:
            results = [before_cutoff(w[0]) for w in windows]
            climatology = [cutoff_climatology(before_cutoff, w[0], years=others, **pooling) for w in windows]
            positive, negative = runs([r for r in results if r != obs_core.UNVERIFIABLE])
        else:
            raise ValueError(f"unknown event {event!r}")
        slots = tuple(Slot(issue.isoformat(), result, c.probability)
                      for issue, result, c in zip(issues, results, climatology)
                      if result != obs_core.UNVERIFIABLE)
        out.append(Season(year, slots, positive, negative))
    return out


def episodes(seasons: Sequence[Season]) -> tuple[int, int]:
    return sum(s.positive for s in seasons), sum(s.negative for s in seasons)


# --- simulated records --------------------------------------------------------


def draw_forecasts(rng: np.random.Generator, slots: Sequence[Slot], skill: float) -> list[float]:
    """A calibrated forecast of expected skill ``skill`` for each slot, given its outcome."""
    if not 0 < skill < 1:
        raise ValueError("a simulated skill must be inside 0-1")
    spread = (1 - skill) / skill
    c = np.array([s.climatology for s in slots])
    held = np.array([s.outcome == obs_core.HELD for s in slots], dtype=float)
    return rng.beta(c * spread + held, (1 - c) * spread + 1 - held).tolist()


def rows(slots: Sequence[Slot], probabilities: Sequence[float]) -> list[score_core.Row]:
    return score_core.rows(
        {"issue_date": s.issue_date, "outcome": s.outcome, "probability": p,
         "climatology_probability": s.climatology}
        for s, p in zip(slots, probabilities)
    )


def simulate(hindcast: Sequence[Season], pool: Sequence[Season], skill: float, rules: score_core.Rules,
             design: Design, rng: np.random.Generator) -> Power:
    """Simulated hindcasts over ``hindcast``, each with one live season drawn from ``pool``.

    The hindcast and the first live season both have true skill ``skill``; the
    second live season states climatology, so its skill is exactly zero.
    """
    slots = [slot for s in hindcast for slot in s.slots]
    pool = [s for s in pool if s.slots]
    passes = dict.fromkeys(design.confidences, 0)
    keys = [(c, m) for c in design.confidences for m in design.margins]
    live_equal = {key: Counter() for key in keys}
    live_none = {key: Counter() for key in keys}
    for _ in range(design.replicates):
        hindcast_rows = rows(slots, draw_forecasts(rng, slots, skill))
        live = pool[rng.integers(len(pool))].slots
        equal_rows = rows(live, draw_forecasts(rng, live, skill))
        none_rows = rows(live, [s.climatology for s in live])
        for confidence in design.confidences:
            at = dataclasses.replace(rules, confidence=confidence, bootstrap_resamples=design.bootstrap_resamples)
            result = score_core.skill(hindcast_rows, score_core.season, at)
            # N is taken as met here; the episode counts say separately whether it is.
            met = at.min_episodes
            passes[confidence] += score_core.hindcast_verdict(result, met, met, at) == score_core.PASS
            equal = score_core.skill_difference(hindcast_rows, equal_rows, at)
            none = score_core.skill_difference(hindcast_rows, none_rows, at)
            for margin in design.margins:
                with_margin = dataclasses.replace(at, noninferiority_margin=margin)
                live_equal[confidence, margin][score_core.live_check(result, equal, with_margin)] += 1
                live_none[confidence, margin][score_core.live_check(result, none, with_margin)] += 1
    return Power(design.replicates, passes, live_equal, live_none)


def seasons_until_judged(pool: Sequence[Season], min_episodes: int, design: Design,
                         rng: np.random.Generator) -> tuple[float, float] | None:
    """(median, 90th percentile) of live seasons until both episode counts reach ``min_episodes``.

    Seasons are drawn from ``pool`` with replacement. None when fewer than 90%
    of the draws get there within ``design.season_cap`` seasons.
    """
    counts = np.array([(s.positive, s.negative) for s in pool])
    drawn = counts[rng.integers(0, len(counts), size=(design.replicates, design.season_cap))].cumsum(axis=1)
    reached = (drawn >= min_episodes).all(axis=2)
    needed = np.where(reached.any(axis=1), reached.argmax(axis=1) + 1, np.inf)
    median, late = np.quantile(needed, [0.5, 0.9], method="inverted_cdf")
    return None if math.isinf(late) else (float(median), float(late))


def minimum_detectable(rates: Mapping[float, float], target: float) -> float | None:
    """The smallest simulated skill whose pass rate reaches ``target``; None when none does."""
    return min((skill for skill, rate in rates.items() if rate >= target), default=None)


# --- the table ----------------------------------------------------------------


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def _mm(threshold: float) -> str:
    return f"{threshold:g} mm"


def _range(seasons: Sequence[Season]) -> str:
    return f"{seasons[0].year}–{seasons[-1].year}"


def _listed(values: Sequence[float]) -> str:
    return ", ".join(f"{v:g}" for v in values)


def power(ledger_root: Path, design: Design = Design()) -> str:
    """The power table as Markdown, from the saved final records the dev bundle names."""
    bundle = ledger_root / observations.DEV_BUNDLE_DIR
    sem = semantics.load(bundle / "bundle.toml")
    obs_rules = obs_core.load_rules(bundle / "observations.toml")
    rules = score_core.load_rules(bundle / "scoring.toml")
    vintage = rules.climatology_vintage
    band_map = observations.load_band_map(ledger_root)
    series = observations.load(ledger_root, vintage)
    years = [y for y in observations.full_years(series) if y >= obs_rules.climatology_first_year]
    cutoff = f"{obs_rules.sowing_cutoff[1]} {date(2000, obs_rules.sowing_cutoff[0], 1):%b}"
    files = observations.record_files(ledger_root, vintage)

    out = [
        "# Mandi wheat power simulation", "",
        "Generated by `evidence bundle power`; do not edit. The procedure is",
        "`docs/findings/2026-10-mandi-power-simulation-procedure.md`. Outcomes and climatology come",
        "from the committed IMD files named below, with the development rule values; forecasts are",
        "simulated at a stated true skill. No real forecast enters this table.", "",
        "## Inputs", "",
        f"- Observations: IMD 0.25° gridded daily rainfall, vintage `{vintage}`,",
        f"  {len(files)} files under `observations/`,",
        f"  set digest `{observations.files_digest(files)}`.",
        f"- Band map: `{bands.BAND_MAP_PATH}`, SHA-256",
        f"  `{hashlib.sha256((ledger_root / bands.BAND_MAP_PATH).read_bytes()).hexdigest()}`.",
        f"- Seasons: {years[0]} to {years[-1]} ({len(years)}); climatology leaves the scored season out.",
        f"- Issue dates: each Monday from {sem.season_start[1]} Oct to {sem.season_end[1]} Nov; "
        f"{sem.rain_days}-day window; sowing cutoff {cutoff}.",
        f"- Coverage share {obs_rules.coverage_share:g}; wet day {_mm(obs_rules.wet_day_mm)}; dry-day gap "
        f"{obs_rules.dry_day_gap_days} days;",
        f"  climatology ±{obs_rules.climatology_half_window_days} days with pseudo-count "
        f"{obs_rules.climatology_pseudo_count:g}.",
        f"- Pass needs the lower skill bound above {rules.skill_threshold:g}. Seed {rules.bootstrap_seed};",
        f"  {design.replicates} simulated records per row, {design.bootstrap_resamples} bootstrap resamples each.",
        f"- Candidates: thresholds {_listed(design.thresholds_mm)} mm; minimum N {_listed(design.min_episodes)};",
        f"  confidence {_listed(design.confidences)}; margin {_listed(design.margins)};",
        f"  true skill {_listed(design.skills)}.", "",
    ]
    for b, verdict_band in enumerate(band_map.verdict_bands):
        band = obs_core.band_daily(series, band_map, verdict_band, obs_rules.coverage_share)
        pools = {(e, t): observed_seasons(band, years, sem, obs_rules, event, threshold)
                 for e, event in enumerate(EVENTS) for t, threshold in enumerate(design.thresholds_mm)}
        # One job per event, threshold, season range and true skill, each with its own seed, so the
        # table does not depend on how many processes share the work.
        jobs = {
            (e, t, r, k): ([s for s in pool if s.year >= first], pool, skill, rules, design,
                           [rules.bootstrap_seed, 1, b, e, t, r, k])
            for (e, t), pool in pools.items()
            for r, first in enumerate(design.simulated_first_years)
            for k, skill in enumerate(design.skills)
        }
        with ProcessPoolExecutor() as workers:
            results = dict(zip(jobs, workers.map(_simulate_seeded, jobs.values())))
        out += [f"## Verdict band `{verdict_band}`", ""]
        for e, event in enumerate(EVENTS):
            out += _event_tables(event, (b, e), pools, results, sem, cutoff, rules, design)
    return "\n".join(out)


def _simulate_seeded(job) -> Power:
    *arguments, seed = job
    return simulate(*arguments, np.random.default_rng(seed))


def _event_tables(event, key, pools, results, sem, cutoff, rules, design) -> list[str]:
    b, e = key
    counted, until, passing, live = [], [], [], []
    for t, threshold in enumerate(design.thresholds_mm):
        pool = pools[e, t]
        ranges = {first: [s for s in pool if s.year >= first] for first in design.counted_first_years}
        for seasons in (pool, *ranges.values()):
            slots = [slot for s in seasons for slot in s.slots]
            held = sum(slot.outcome == obs_core.HELD for slot in slots)
            positive, negative = episodes(seasons)
            met = max((n for n in design.min_episodes if min(positive, negative) >= n), default=None)
            counted.append(
                f"| {_mm(threshold)} | {_range(seasons)} | {len(seasons)} | {len(slots)} | {held} "
                f"| {_pct(held, len(slots))} | {positive} | {negative} | {met or 'none'} |"
            )

        positive, negative = episodes(pool)
        cells = []
        for n, min_episodes in enumerate(design.min_episodes):
            rng = np.random.default_rng([rules.bootstrap_seed, 0, b, e, t, n])
            needed = seasons_until_judged(pool, min_episodes, design, rng)
            cells.append(f"over {design.season_cap}" if needed is None else f"{needed[0]:g} ({needed[1]:g})")
        until.append(f"| {_mm(threshold)} | {positive / len(pool):.2f} | {negative / len(pool):.2f} | "
                     + " | ".join(cells) + " |")

        for r, first in enumerate(design.simulated_first_years):
            seasons = ranges[first]
            by_skill = {skill: results[e, t, r, k] for k, skill in enumerate(design.skills)}
            for confidence in design.confidences:
                rates = {skill: p.passes[confidence] / p.replicates for skill, p in by_skill.items()}
                least = minimum_detectable(rates, design.power_target)
                passing.append(
                    f"| {_mm(threshold)} | {_range(seasons)} | {confidence:g} | "
                    + " | ".join(f"{100 * rate:.0f}%" for rate in rates.values())
                    + f" | {'above ' + format(max(design.skills), 'g') if least is None else format(least, 'g')} |"
                )
                for margin in design.margins:
                    cells = [
                        " / ".join(_pct(tally[verdict], p.replicates) for tally, verdict in (
                            (p.live_equal[confidence, margin], score_core.FAIL),
                            (p.live_equal[confidence, margin], score_core.PASS),
                            (p.live_none[confidence, margin], score_core.FAIL),
                        ))
                        for p in by_skill.values()
                    ]
                    live.append(f"| {_mm(threshold)} | {_range(seasons)} | {confidence:g} | {margin:g} | "
                                + " | ".join(cells) + " |")

    title = (f"Rain in the {sem.rain_days}-day window" if event == WINDOW
             else f"Next rain before the sowing cutoff ({cutoff})")
    skills = " | ".join(f"{s:g}" for s in design.skills)
    columns = "---|" * len(design.skills)
    return [
        f"### {title}", "",
        "#### Episodes in each season range", "",
        "The first range is every season; the others are candidate hindcast ranges. The last",
        "column is the largest candidate minimum N that both episode counts reach.", "",
        "| Threshold | Seasons | Count | Issue dates | Held | Base rate | Positive episodes "
        "| Negative episodes | Largest minimum N met |",
        "|---|---|---|---|---|---|---|---|---|", *counted, "",
        "#### Live seasons until a band can be judged", "",
        "Seasons drawn with replacement from every season until positive and negative episodes",
        "both reach the minimum N: median, with the 90th percentile in brackets.", "",
        "| Threshold | Positive per season | Negative per season | "
        + " | ".join(f"N = {n}" for n in design.min_episodes) + " |",
        "|---|---|---|" + "---|" * len(design.min_episodes), *until, "",
        "#### Hindcast pass rate by true skill", "",
        "Share of simulated hindcasts whose lower skill bound passes, with N taken as met. The",
        f"minimum detectable skill is the smallest that passes at least {design.power_target:.0%} of the time.", "",
        f"| Threshold | Seasons | Confidence | {skills} | Minimum detectable |",
        "|---|---|---|" + columns + "---|", *passing, "",
        "#### Live check by true hindcast skill", "",
        "One live season drawn from every season. Each cell is: fails when the live season has the",
        "hindcast's skill (a false fail) / passes then / fails when the live season states climatology.", "",
        f"| Threshold | Seasons | Confidence | Margin | {skills} |",
        "|---|---|---|---|" + columns, *live, "",
    ]


def write_power(*, repo: Path, check: bool, design: Design = Design()) -> int:
    """Write the table, or with ``check`` exit 1 unless the committed one matches."""
    ledger_root = repo / "ledger" / observations.LEDGER
    text = power(ledger_root, design)
    path = ledger_root / POWER_PATH
    if check:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            print(f"reproduced {path.relative_to(repo)}")
            return 0
        print(f"{path.relative_to(repo)} differs from the table the committed files give", file=sys.stderr)
        return 1
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path.relative_to(repo)}")
    return 0

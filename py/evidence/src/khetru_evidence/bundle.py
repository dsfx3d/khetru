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
skill score against climatology is ``s``. The draws of a season are made twice
over: independent of one another, and ``shared``, where one draw decides how
good every forecast of the season is. Real forecast errors lie between the two.
Every verdict is then read with the ``score_core`` functions the real record
will use.

A ``Variant`` is a recount made after the first run with some development
values changed in code; no bundle is written for it. It has its own output
file and its own seed positions, and the first run's table does not change.
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
from scipy.special import betaincinv

from khetru_evidence import bands, obs_core, observations, score_core, semantics, views

POWER_PATH = "power.md"
WINDOW, BEFORE_CUTOFF = "window", "before_cutoff"
EVENTS = (WINDOW, BEFORE_CUTOFF)
# How a season's simulated forecast errors relate to one another.
INDEPENDENT, SHARED = "independent", "shared"
ERRORS = (INDEPENDENT, SHARED)
# "Rain before the cutoff" for a window that starts too late for a run of its length to end by the
# cutoff. Such an issue date is left out of that event, as an unverifiable one is.
NO_ROOM = "no_room"
JUDGED = (obs_core.HELD, obs_core.NOT_HELD)


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
class Variant:
    """A run of the simulation: the first one, or a later recount with some development values changed."""

    name: str | None  # what `evidence bundle power --variant` takes; None for the first run
    path: str  # the table, under the ledger root
    made: str = ""  # the date a recount was made
    season_end: tuple[int, int] | None = None  # replaces the development value when given
    sowing_cutoff: tuple[int, int] | None = None
    views: bool = False  # whether the exploratory views are counted after the verdict bands
    # Second number of every seed. A variant has its own two, so none of its rows shares a seed.
    until_stream: int = 0
    simulate_stream: int = 1


FIRST_RUN = Variant(None, POWER_PATH)
# Issue dates on every Monday to 14 Dec and a cutoff of 16 Dec: the last sowing week in the CSK HPKV
# guidance is 10-16 Dec. Evidence for the owner's choice of issue dates; not adopted values.
DECEMBER = Variant("december", "power-december.md", made="2026-10-11", season_end=(12, 14),
                   sowing_cutoff=(12, 16), views=True, until_stream=2, simulate_stream=3)
VARIANTS = {v.name: v for v in (DECEMBER,)}


def values(variant: Variant, sem: semantics.Semantics,
           obs_rules: obs_core.Rules) -> tuple[semantics.Semantics, obs_core.Rules]:
    """The development values with ``variant``'s changes."""
    if variant.season_end:
        sem = dataclasses.replace(sem, season_end=variant.season_end)
    if variant.sowing_cutoff:
        obs_rules = dataclasses.replace(obs_rules, sowing_cutoff=variant.sowing_cutoff)
    return sem, obs_rules


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
    unverifiable: int = 0  # issue dates left out for a date with no value
    no_room: int = 0  # issue dates left out of "rain before the cutoff" for starting too late


@dataclass(frozen=True)
class Power:
    """What ``replicates`` simulated records gave for one event, threshold, range, true skill and errors."""

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
            if result in JUDGED:
                windows += 1
                events += result == obs_core.HELD
    return obs_core.Climatology((events + pseudo_count) / (windows + 2 * pseudo_count), events, windows)


def has_room(first: date, days: int, cutoff: tuple[int, int]) -> bool:
    """Whether a run of ``days`` dates starting on ``first`` ends by the cutoff of its year."""
    return first + timedelta(days=days - 1) <= date(first.year, *cutoff)


def rain_before_cutoff(band: Mapping[date, float], days: int, threshold_mm: float,
                       cutoff: tuple[int, int]) -> Callable[[date], str]:
    """The outcome of "rain before the cutoff" by the window's first date, worked out once per date.

    ``no_room`` when no run of ``days`` dates fits between that date and the
    cutoff; ``obs_core.rain_before`` alone would call it not held.
    """
    @functools.cache
    def outcome(first: date) -> str:
        if not has_room(first, days, cutoff):
            return NO_ROOM
        return obs_core.rain_before(band, first, date(first.year, *cutoff), days, threshold_mm)
    return outcome


def used_dates(sem: semantics.Semantics, rules: obs_core.Rules, year: int) -> tuple[date, date]:
    """The first and last observed date a season's windows and their climatology can use."""
    issues = semantics.issue_dates(sem, year)
    first, last = (obs_core.imd_dates(semantics.schedule(sem, issue)) for issue in (issues[0], issues[-1]))
    pooled = timedelta(days=rules.climatology_half_window_days)
    return first[0] - pooled, max(last[-1] + pooled, date(year, *rules.sowing_cutoff))


def short_seasons(have, years: Sequence[int], sem: semantics.Semantics, rules: obs_core.Rules) -> dict[int, int]:
    """Seasons with a used date that is not among the dates ``have``, with how many are not."""
    short = {}
    for year in years:
        first, last = used_dates(sem, rules, year)
        gone = sum(d not in have for d in obs_core.run_dates(first, (last - first).days + 1))
        if gone:
            short[year] = gone
    return short


def observed_seasons(band: Mapping[date, float], years: Sequence[int], sem: semantics.Semantics,
                     rules: obs_core.Rules, event: str, threshold_mm: float) -> list[Season]:
    """Each season's issue dates for ``event``, with leave-one-season-out climatology from ``years``.

    ``window`` is rain in the observed window, with ``obs_core``'s episodes.
    ``before_cutoff`` is some run of the window's length reaching the threshold
    between the window's first date and the sowing cutoff. A season's held issue
    dates then lean on the same rain, so its episodes are the runs of held and
    of not-held issue dates: at most one of each. An issue date with no room
    for such a run before the cutoff is left out, of climatology too.
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
            positive, negative = runs([r for r in results if r in JUDGED])
        else:
            raise ValueError(f"unknown event {event!r}")
        slots = tuple(Slot(issue.isoformat(), result, c.probability)
                      for issue, result, c in zip(issues, results, climatology)
                      if result in JUDGED)
        out.append(Season(year, slots, positive, negative, results.count(obs_core.UNVERIFIABLE),
                          results.count(NO_ROOM)))
    return out


def episodes(seasons: Sequence[Season]) -> tuple[int, int]:
    return sum(s.positive for s in seasons), sum(s.negative for s in seasons)


# --- simulated records --------------------------------------------------------


def draw_forecasts(rng: np.random.Generator, slots: Sequence[Slot], skill: float,
                   errors: str = INDEPENDENT) -> list[float]:
    """A calibrated forecast of expected skill ``skill`` for each slot, given its outcome.

    With ``shared`` errors one draw per season says how good its forecasts are:
    each slot takes that quantile of its own distribution, counted from the
    wrong end, so a season's forecasts are all as good or as bad as one another.
    Each forecast keeps the distribution it has when drawn independently.
    """
    if not 0 < skill < 1:
        raise ValueError("a simulated skill must be inside 0-1")
    spread = (1 - skill) / skill
    c = np.array([s.climatology for s in slots])
    held = np.array([s.outcome == obs_core.HELD for s in slots], dtype=float)
    a, b = c * spread + held, (1 - c) * spread + 1 - held
    if errors == INDEPENDENT:
        return rng.beta(a, b).tolist()
    if errors != SHARED:
        raise ValueError(f"unknown errors {errors!r}")
    seasons = sorted({s.issue_date[:4] for s in slots})
    quality = dict(zip(seasons, rng.random(len(seasons))))
    good = np.array([quality[s.issue_date[:4]] for s in slots])
    # A good forecast is high when the event held and low when it did not.
    return betaincinv(a, b, np.where(held == 1, good, 1 - good)).tolist()


def rows(slots: Sequence[Slot], probabilities: Sequence[float]) -> list[score_core.Row]:
    return score_core.rows(
        {"issue_date": s.issue_date, "outcome": s.outcome, "probability": p,
         "climatology_probability": s.climatology}
        for s, p in zip(slots, probabilities)
    )


def simulate(hindcast: Sequence[Season], pool: Sequence[Season], skill: float, errors: str,
             rules: score_core.Rules, design: Design, rng: np.random.Generator) -> Power:
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
        hindcast_rows = rows(slots, draw_forecasts(rng, slots, skill, errors))
        live = pool[rng.integers(len(pool))].slots
        equal_rows = rows(live, draw_forecasts(rng, live, skill, errors))
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


def jobs(pools: Mapping[tuple[int, int], Sequence[Season]], b: int, rules: score_core.Rules, design: Design,
         variant: Variant = FIRST_RUN) -> dict[tuple, tuple]:
    """The simulations of area ``b``: one per event, threshold, season range, true skill and kind of errors.

    Each has its own seed, the last item, so the table does not depend on how
    many processes share the work.
    """
    return {
        (e, t, r, k, x): ([s for s in pool if s.year >= first], pool, skill, errors, rules, design,
                          [rules.bootstrap_seed, variant.simulate_stream, b, e, t, r, k, x])
        for (e, t), pool in pools.items()
        for r, first in enumerate(design.simulated_first_years)
        for k, skill in enumerate(design.skills)
        for x, errors in enumerate(ERRORS)
    }


def until_seed(rules: score_core.Rules, variant: Variant, b: int, e: int, t: int, n: int) -> list[int]:
    """The seed of one cell of "live seasons until a band can be judged"."""
    return [rules.bootstrap_seed, variant.until_stream, b, e, t, n]


def power(ledger_root: Path, design: Design = Design(), variant: Variant = FIRST_RUN) -> str:
    """The power table as Markdown, from the saved final records the dev bundle names."""
    bundle = ledger_root / observations.DEV_BUNDLE_DIR
    dev = semantics.load(bundle / "bundle.toml"), obs_core.load_rules(bundle / "observations.toml")
    sem, obs_rules = values(variant, *dev)
    rules = score_core.load_rules(bundle / "scoring.toml")
    vintage = rules.climatology_vintage
    main = views.Area(ledger_root / bands.BAND_MAP_PATH, observations.load_band_map(ledger_root))
    areas = {band: area for area in ([main, *views.areas(ledger_root)[1:]] if variant.views else [main])
             for band in area.band_map.verdict_bands}
    series = observations.load(ledger_root, vintage)
    years = [y for y in observations.full_years(series) if y >= obs_rules.climatology_first_year]
    short = short_seasons(set(series.dates), years, sem, obs_rules)
    years = [y for y in years if y not in short]
    cutoff = observations.day_month(obs_rules.sowing_cutoff)
    files = observations.record_files(ledger_root, vintage)
    command = "evidence bundle power" + (f" --variant {variant.name}" if variant.name else "")
    daily = {band: obs_core.band_daily(series, area.band_map, band, obs_rules.coverage_share)
             for band, area in areas.items()}
    pools = {band: {(e, t): observed_seasons(daily[band], years, sem, obs_rules, event, threshold)
                    for e, event in enumerate(EVENTS) for t, threshold in enumerate(design.thresholds_mm)}
             for band in areas}

    out = [
        "# Mandi wheat power simulation" + (f": {variant.name.capitalize()} recount" if variant.name else ""), "",
        f"Generated by `{command}`; do not edit. The procedure is",
        "`docs/findings/2026-10-mandi-power-simulation-procedure.md`. Outcomes and climatology come",
        "from the committed IMD files named below, with the development rule values; forecasts are",
        "simulated at a stated true skill. No real forecast enters this table.", "",
        *(_recount_note(variant, dev, sem, obs_rules) if variant.name else ()),
        "## Inputs", "",
        f"- Observations: IMD 0.25° gridded daily rainfall, vintage `{vintage}`,",
        f"  {len(files)} files under `observations/`,",
        f"  set digest `{observations.files_digest(files)}`.",
        *(line for area in dict.fromkeys(areas.values()) for line in (
            f"- {'Band map' if area is main else 'Exploratory view'}: "
            f"`{area.path.relative_to(ledger_root).as_posix()}`, SHA-256",
            f"  `{hashlib.sha256(area.path.read_bytes()).hexdigest()}`.",
        )),
        f"- Seasons: {years[0]} to {years[-1]} ({len(years)}); climatology leaves the scored season out.",
        f"- Issue dates: each Monday from {observations.day_month(sem.season_start)} to "
        f"{observations.day_month(sem.season_end)}; "
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
        *(_recount_checks(series, areas, pools, years, short, sem, obs_rules, design) if variant.name else ()),
    ]
    for b, (band, area) in enumerate(areas.items()):
        work = jobs(pools[band], b, rules, design, variant)
        with ProcessPoolExecutor() as workers:
            results = dict(zip(work, workers.map(_simulate_seeded, work.values())))
        if area is main:
            out += [f"## Verdict band `{band}`", ""]
        else:
            cells = ", ".join(f"{c.lat:.2f}N {c.lon:.2f}E" for c in area.band_map.cells)
            out += [f"## Exploratory view `{band}`", "",
                    "Not a verdict band: a view is read beside the verdict bands and never decides a verdict.",
                    f"IMD cells: {cells}.", ""]
        for e, event in enumerate(EVENTS):
            out += _event_tables(event, (b, e), pools[band], results, sem, cutoff, rules, design, variant)
    return "\n".join(out)


def _recount_note(variant, dev, sem, obs_rules) -> list[str]:
    day = observations.day_month
    return [
        f"This is a recount made on {variant.made}, after the first run, whose table is `{POWER_PATH}` and",
        "is unchanged. It is listed under \"Changes after the run\" in the procedure. Two development",
        f"values are changed in code, and no bundle holds them: the last issue date is {day(sem.season_end)}",
        f"where the first run has {day(dev[0].season_end)}, and the sowing cutoff is {day(obs_rules.sowing_cutoff)} "
        f"where it has",
        f"{day(dev[1].sowing_cutoff)}. Every other value is the first run's. The exploratory views are counted after",
        "the verdict band. Each row has a seed of its own, shared with no row of either table.", "",
        "The owner has not chosen these values. The cutoff, the last issue date and the rule for",
        "issue dates with no room before the cutoff (below) are the agent's and open for the owner.", "",
    ]


def _recount_checks(series, areas, pools, years, short, sem, obs_rules, design) -> list[str]:
    """What a recount with later issue dates has to show about the observations it newly reads."""
    day = observations.day_month
    spans = [used_dates(sem, obs_rules, y) for y in years]
    first, last = min((a.month, a.day) for a, _ in spans), max((b.month, b.day) for _, b in spans)
    late = [m for m in range(first[0], last[0] + 1) if m not in observations.SEASON_MONTHS]
    names = " and ".join(f"{date(2000, m, 1):%B}" for m in late)
    out = ["## Observations the later issue dates use", ""]
    if short:
        out += [f"- Left out, for lacking a date their windows or climatology use ({day(first)} to {day(last)}",
                "  at the widest): " + ", ".join(f"{y} ({n} dates)" for y, n in short.items()) + "."]
    else:
        out += [f"- Every season from {years[0]} to {years[-1]} has every date its windows and their climatology",
                f"  use ({day(first)} to {day(last)} at the widest)."]
    cutoff_event = next(iter(pools.values()))[EVENTS.index(BEFORE_CUTOFF), 0]
    slots = sum(len(semantics.issue_dates(sem, y)) for y in years)
    gone = sum(s.no_room for s in cutoff_event)
    most = max(s.no_room for s in cutoff_event)
    none = [str(s.year) for s in cutoff_event if not s.no_room]
    latest = date(2000, *obs_rules.sowing_cutoff) - timedelta(days=sem.rain_days - 1)
    out += [
        f"- Rain before the cutoff needs {sem.rain_days} dates in a row from the window's first date to the cutoff.",
        f"  A window that starts after {day((latest.month, latest.day))} has no room for them. Its issue date is "
        "left out of",
        "  that event and of that event's climatology, as an unverifiable one is, and is not counted",
        f"  as not held. This leaves out {gone} of {slots} issue dates: at most {most} in a season, and none in",
        f"  {', '.join(none) if none else 'no season'}. The 7-day event keeps every issue date.", "",
        "Missing cell values over each area's cells, in every saved year:", "",
        "| Area | Cells | Cell-days, October and November | Missing | Days with a missing cell "
        f"| Cell-days, {names} | Missing | Days with a missing cell |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for band, area in areas.items():
        early = observations.missing_cells(series, area.band_map)
        later = observations.missing_cells(series, area.band_map, tuple(late))
        out.append(f"| `{band}` | {len(area.band_map.cells)} | {early[1]} | {early[0]} | {early[2]} "
                   f"| {later[1]} | {later[0]} | {later[2]} |")
    thresholds = ", ".join(_mm(t) for t in design.thresholds_mm)
    out += ["", "Unverifiable issue dates, left out for a date with no band value:", "",
            f"| Area | 7-day event | Rain before the cutoff, at {thresholds} |",
            "|---|---|---|"]
    for band in areas:
        window = sum(s.unverifiable for s in pools[band][EVENTS.index(WINDOW), 0])
        before = " / ".join(str(sum(s.unverifiable for s in pools[band][EVENTS.index(BEFORE_CUTOFF), t]))
                            for t in range(len(design.thresholds_mm)))
        out.append(f"| `{band}` | {window} | {before} |")
    return [*out, ""]


def _simulate_seeded(job) -> Power:
    *arguments, seed = job
    return simulate(*arguments, np.random.default_rng(seed))


def _event_tables(event, key, pools, results, sem, cutoff, rules, design, variant=FIRST_RUN) -> list[str]:
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
            rng = np.random.default_rng(until_seed(rules, variant, b, e, t, n))
            needed = seasons_until_judged(pool, min_episodes, design, rng)
            cells.append(f"over {design.season_cap}" if needed is None else f"{needed[0]:g} ({needed[1]:g})")
        until.append(f"| {_mm(threshold)} | {positive / len(pool):.2f} | {negative / len(pool):.2f} | "
                     + " | ".join(cells) + " |")

        for r, first in enumerate(design.simulated_first_years):
            seasons = ranges[first]
            for x, errors in enumerate(ERRORS):
                by_skill = {skill: results[e, t, r, k, x] for k, skill in enumerate(design.skills)}
                for confidence in design.confidences:
                    row = f"| {_mm(threshold)} | {_range(seasons)} | {errors} | {confidence:g} | "
                    rates = {skill: p.passes[confidence] / p.replicates for skill, p in by_skill.items()}
                    least = minimum_detectable(rates, design.power_target)
                    passing.append(
                        row + " | ".join(f"{100 * rate:.0f}%" for rate in rates.values())
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
                        live.append(row + f"{margin:g} | " + " | ".join(cells) + " |")

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
        f"minimum detectable skill is the smallest that passes at least {design.power_target:.0%} of the time.",
        "A season's forecast errors are drawn independently, or shared so that its forecasts are",
        "all as good or as bad as one another; real errors lie between the two.", "",
        f"| Threshold | Seasons | Errors | Confidence | {skills} | Minimum detectable |",
        "|---|---|---|---|" + columns + "---|", *passing, "",
        "#### Live check by true hindcast skill", "",
        "One live season drawn from every season. Each cell is: fails when the live season has the",
        "hindcast's skill (a false fail) / passes then / fails when the live season states climatology.", "",
        f"| Threshold | Seasons | Errors | Confidence | Margin | {skills} |",
        "|---|---|---|---|---|" + columns, *live, "",
    ]


def write_power(*, repo: Path, check: bool, design: Design = Design(), variant: Variant = FIRST_RUN) -> int:
    """Write ``variant``'s table, or with ``check`` exit 1 unless the committed one matches."""
    ledger_root = repo / "ledger" / observations.LEDGER
    text = power(ledger_root, design, variant)
    path = ledger_root / variant.path
    if check:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            print(f"reproduced {path.relative_to(repo)}")
            return 0
        print(f"{path.relative_to(repo)} differs from the table the committed files give", file=sys.stderr)
        return 1
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path.relative_to(repo)}")
    return 0

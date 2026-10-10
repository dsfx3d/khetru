"""The scoring rule: one score body per claim and observation vintage, and the verdict quantities.

Pure; frozen at the bundle tag (KTD9). ``score_claim`` reads no file and knows
no ``kind``: the live, exploratory and hindcast drivers all call it and add
their own ledger fields, so the same inputs give the same body on every path.

A score holds the observed outcome, the probability that is scored and
climatology's probability for the same band, window and threshold (R7, R8). The
Brier scores are not stored; ``brier_pair`` derives them from those three. An
abstain, a ``not_issued`` slot, a voided claim and a late claim are scored as
if they had stated climatology's probability, so they add no skill and still
count (KTD7). An ``unverifiable`` outcome has no Brier score.

The rest computes what the pass/fail rule reads (R13, R17, KTD8, KTD13): the
Brier skill score against climatology, cluster bootstrap intervals, the three
verdicts of the decision table, and Richardson's relative economic value.
"""

import math
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from khetru_evidence import obs_core
from khetru_evidence.ledger import timestamp
from khetru_evidence.semantics import Semantics, schedule

PROBABILITY_DECIMALS = 6
MM_DECIMALS = 6

CLAIM, CLIMATOLOGY = "claim", "climatology"
# Why a slot is scored as climatology, most decisive first.
VOIDED, NOT_ISSUED, ABSTAIN, LATE = "voided", "not_issued", "abstain", "late"

# Timing tests (KTD3) and their results.
OPENTIMESTAMPS, FORECAST_AVAILABILITY = "opentimestamps", "forecast_availability"
TIMING_TESTS = (OPENTIMESTAMPS, FORECAST_AVAILABILITY)
ON_TIME, PENDING = "on_time", "pending"

PASS, FAIL, TOO_EARLY = "pass", "fail", "too_early_to_tell"

_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"


class ScoreError(ValueError):
    """A score cannot be made, or a verdict quantity computed, from these inputs."""


@dataclass(frozen=True)
class Rules:
    """Scoring and pass/fail rule values, read from a bundle's ``[scoring]`` table."""

    climatology_vintage: str
    skill_threshold: float  # the lower skill bound must be above this to pass
    min_episodes: int  # independent rain episodes needed, positive and negative each
    confidence: float  # two-sided level of the bootstrap intervals
    noninferiority_margin: float
    bootstrap_seed: int
    bootstrap_resamples: int
    cost_loss_ratios: tuple[float, ...]


def load_rules(toml_path: Path) -> Rules:
    with open(toml_path, "rb") as f:
        s = tomllib.load(f)["scoring"]
    rules = Rules(
        climatology_vintage=str(s["climatology_vintage"]),
        skill_threshold=float(s["skill_threshold"]),
        min_episodes=int(s["min_episodes"]),
        confidence=float(s["confidence"]),
        noninferiority_margin=float(s["noninferiority_margin"]),
        bootstrap_seed=int(s["bootstrap_seed"]),
        bootstrap_resamples=int(s["bootstrap_resamples"]),
        cost_loss_ratios=tuple(float(r) for r in s["cost_loss_ratios"]),
    )
    if not rules.climatology_vintage.startswith("final-"):
        raise ScoreError("climatology_vintage must be an IMD final vintage")
    if not 0 < rules.confidence < 1:
        raise ScoreError("confidence must be inside 0-1")
    if rules.min_episodes < 1 or rules.bootstrap_resamples < 1:
        raise ScoreError("min_episodes and bootstrap_resamples must be at least 1")
    if not rules.noninferiority_margin > 0:
        raise ScoreError("noninferiority_margin must be above 0")
    if not rules.cost_loss_ratios or not all(0 < r < 1 for r in rules.cost_loss_ratios):
        raise ScoreError("cost_loss_ratios must hold at least one ratio, each inside 0-1")
    return rules


# --- one claim, one vintage --------------------------------------------------


def timing(sem: Semantics, claim: Mapping, test: str, attested_at: datetime | None = None) -> str:
    """Whether a claim was made in time, by ``test`` (KTD3): on time, late or pending.

    ``opentimestamps`` is for claims written in real time: the claim's manifest
    must be attested before the window starts, and until a proof says when, the
    timing is pending. ``forecast_availability`` is for hindcast and retroactive
    claims: the run the claim names must be available by the time the issue
    date's own run is.
    """
    times = schedule(sem, date.fromisoformat(claim["issue_date"]))
    if test == OPENTIMESTAMPS:
        if attested_at is None:
            return PENDING
        return ON_TIME if attested_at < times.late_cutoff else LATE
    if test == FORECAST_AVAILABILITY:
        run_init = datetime.strptime(claim["run_init"], _TIMESTAMP).replace(tzinfo=UTC)
        available = run_init + timedelta(hours=sem.run_available_after_hours)
        return ON_TIME if available <= times.run_available else LATE
    raise ScoreError(f"unknown timing test {test!r}")


def score_claim(*, sem: Semantics, threshold_mm: float, slot: Mapping, voided: bool,
                band: Mapping[date, float], vintage: str, climatology: obs_core.Climatology,
                timing_test: str | None, timing_result: str | None,
                evidence: Mapping[str, str]) -> dict:
    """The score body of one claim, abstain or ``not_issued`` slot against one vintage.

    ``band`` is the verdict band's daily rainfall in ``vintage``. ``timing_test``
    and ``timing_result`` are given for a claim and are None for an abstain or a
    ``not_issued`` slot. ``evidence`` maps each file the caller read to its
    SHA-256 and is copied into the body.
    """
    times = schedule(sem, date.fromisoformat(slot["issue_date"]))
    window = {"window_start": timestamp(times.window_start), "window_end": timestamp(times.window_end)}
    if slot["type"] != NOT_ISSUED:
        stated = {"threshold_mm": slot["threshold_mm"]} | {k: slot[k] for k in window}
        if stated != {"threshold_mm": threshold_mm} | window:
            raise ScoreError(f"{slot['id']}: its threshold or window is not the bundle's")
    if (slot["type"] == CLAIM) != (timing_test is not None):
        raise ScoreError(f"{slot['id']}: a timing test is applied to a claim and to nothing else")
    if timing_test is not None and timing_result not in (ON_TIME, LATE):
        raise ScoreError(f"{slot['id']}: timing is {timing_result}; a score waits for on time or late")

    daily = [round(v, MM_DECIMALS) for v in (band.get(d, math.nan) for d in obs_core.imd_dates(times))]
    total = None if any(math.isnan(v) for v in daily) else round(math.fsum(daily), MM_DECIMALS)
    climatology_probability = round(climatology.probability, PROBABILITY_DECIMALS)
    if not 0 < climatology_probability < 1:
        raise ScoreError("the climatology probability must stay inside 0-1")

    reason = (VOIDED if voided else slot["type"] if slot["type"] != CLAIM
              else LATE if timing_result == LATE else None)
    body = {
        "band": slot["band"],
        "issue_date": slot["issue_date"],
        "threshold_mm": threshold_mm,
        **window,
        "vintage": vintage,
        "provisional": not vintage.startswith("final-"),
        "observed_daily_mm": [None if math.isnan(v) else v for v in daily],
        "observed_mm": total,
        "outcome": obs_core.outcome(total, threshold_mm),
        "scored_as": CLIMATOLOGY if reason else CLAIM,
        "probability": climatology_probability if reason else slot["probability"],
        "climatology_probability": climatology_probability,
        "climatology_events": climatology.events,
        "climatology_windows": climatology.windows,
        "evidence": dict(evidence),
    }
    if reason:
        body["scored_as_reason"] = reason
    if timing_test is not None:
        body |= {"timing_test": timing_test, "timing": timing_result}
    return body


def brier(probability: float, outcome: str) -> float:
    if outcome not in (obs_core.HELD, obs_core.NOT_HELD):
        raise ScoreError(f"a {outcome} outcome has no Brier score")
    return (probability - (outcome == obs_core.HELD)) ** 2


def brier_pair(score: Mapping) -> tuple[float, float] | None:
    """(Brier of the scored probability, Brier of climatology); None when unverifiable."""
    if score["outcome"] == obs_core.UNVERIFIABLE:
        return None
    return brier(score["probability"], score["outcome"]), brier(score["climatology_probability"], score["outcome"])


# --- skill against climatology (KTD8) ----------------------------------------


@dataclass(frozen=True)
class Row:
    """One issue date: Brier scores averaged across the verdict bands that have one."""

    issue_date: str
    brier: float
    brier_climatology: float


@dataclass(frozen=True)
class Skill:
    """Brier skill score with its bootstrap interval; all None when there is nothing to score."""

    bss: float | None
    lower: float | None
    upper: float | None
    clusters: int
    rows: int


def season(row: Row) -> str:
    """The cluster of a hindcast row: its rabi season, named by the year."""
    return row.issue_date[:4]


def issue_date(row: Row) -> str:
    """The cluster of a live row: its issue date."""
    return row.issue_date


def rows(scores: Iterable[Mapping]) -> list[Row]:
    """Each issue date's row from its current scores; unverifiable outcomes are left out (KTD7)."""
    by_date: dict[str, list[tuple[float, float]]] = {}
    for score in scores:
        pair = brier_pair(score)
        if pair is not None:
            by_date.setdefault(score["issue_date"], []).append(pair)
    return [
        Row(day, math.fsum(a for a, _ in pairs) / len(pairs), math.fsum(b for _, b in pairs) / len(pairs))
        for day, pairs in sorted(by_date.items())
    ]


def bss(rows: Sequence[Row]) -> float | None:
    """1 - (mean Brier) / (mean Brier of climatology); None with no rows."""
    if not rows:
        return None
    return 1 - math.fsum(r.brier for r in rows) / math.fsum(r.brier_climatology for r in rows)


def _resampled_bss(rows: Sequence[Row], cluster: Callable[[Row], str], rng: np.random.Generator,
                   resamples: int) -> np.ndarray:
    """BSS of ``resamples`` draws of whole clusters, with replacement."""
    sums: dict[str, list[float]] = {}
    for row in rows:
        total = sums.setdefault(cluster(row), [0.0, 0.0])
        total[0] += row.brier
        total[1] += row.brier_climatology
    table = np.array([sums[key] for key in sorted(sums)])
    drawn = table[rng.integers(0, len(table), size=(resamples, len(table)))].sum(axis=1)
    return 1 - drawn[:, 0] / drawn[:, 1]


def _interval(samples: np.ndarray, confidence: float) -> tuple[float, float]:
    lower, upper = np.quantile(samples, [(1 - confidence) / 2, (1 + confidence) / 2])
    return float(lower), float(upper)


def skill(rows: Sequence[Row], cluster: Callable[[Row], str], rules: Rules) -> Skill:
    """BSS over ``rows`` with a cluster bootstrap interval; the seed and count come from the bundle."""
    if not rows:
        return Skill(None, None, None, 0, 0)
    rng = np.random.default_rng(rules.bootstrap_seed)
    lower, upper = _interval(_resampled_bss(rows, cluster, rng, rules.bootstrap_resamples), rules.confidence)
    return Skill(bss(rows), lower, upper, len({cluster(r) for r in rows}), len(rows))


def skill_difference(hindcast: Sequence[Row], live: Sequence[Row], rules: Rules) -> Skill:
    """Hindcast BSS minus live BSS, resampling seasons and issue dates independently."""
    if not hindcast or not live:
        return Skill(None, None, None, 0, 0)
    rng = np.random.default_rng(rules.bootstrap_seed)
    samples = (_resampled_bss(hindcast, season, rng, rules.bootstrap_resamples)
               - _resampled_bss(live, issue_date, rng, rules.bootstrap_resamples))
    lower, upper = _interval(samples, rules.confidence)
    return Skill(bss(hindcast) - bss(live), lower, upper, len({issue_date(r) for r in live}), len(live))


# --- verdicts: the decision table (R13, R17) ---------------------------------


def _enough(positive: int, negative: int, rules: Rules) -> bool:
    return positive >= rules.min_episodes and negative >= rules.min_episodes


def hindcast_verdict(result: Skill, positive: int, negative: int, rules: Rules) -> str:
    """Pass when N is met and the lower skill bound is above the threshold over climatology."""
    if not _enough(positive, negative, rules) or result.lower is None:
        return TOO_EARLY
    return PASS if result.lower > rules.skill_threshold else FAIL


def live_check(hindcast: Skill, difference: Skill, rules: Rules) -> str:
    """Non-inferiority of live skill to hindcast skill.

    Too early to tell whenever the hindcast's lower skill bound is at or below
    the margin, so a live season with no skill can never pass.
    """
    margin = rules.noninferiority_margin
    if hindcast.lower is None or difference.lower is None or hindcast.lower <= margin:
        return TOO_EARLY
    if difference.upper < margin:
        return PASS
    return FAIL if difference.lower > margin else TOO_EARLY


def gates(hindcast: str, live: str) -> tuple[str, bool]:
    """The STRATEGY gates as (verdict, provisional); a pass is provisional while the live check is too early."""
    if FAIL in (hindcast, live):
        return FAIL, False
    if hindcast == TOO_EARLY:
        return TOO_EARLY, False
    return PASS, live == TOO_EARLY


def band_switch(result: Skill, positive: int, negative: int, values: Iterable[float | None],
                rules: Rules) -> str:
    """Whether a verdict band has earned sharp calls from its accumulated live claims (R17)."""
    if not _enough(positive, negative, rules) or result.lower is None:
        return TOO_EARLY
    worth_acting_on = all(v is not None and v > 0 for v in values)
    return PASS if result.lower > rules.skill_threshold and worth_acting_on else FAIL


# --- cost-loss (KTD13) -------------------------------------------------------


def contingency(scores: Iterable[Mapping], ratio: float) -> tuple[int, int, int, int]:
    """(hits, false alarms, misses, correct rejections), acting when the probability is at least ``ratio``."""
    hits = false_alarms = misses = rejections = 0
    for score in scores:
        if score["outcome"] == obs_core.UNVERIFIABLE:
            continue
        acted, held = score["probability"] >= ratio, score["outcome"] == obs_core.HELD
        hits += acted and held
        false_alarms += acted and not held
        misses += held and not acted
        rejections += not acted and not held
    return hits, false_alarms, misses, rejections


def economic_value(hits: int, false_alarms: int, misses: int, rejections: int, ratio: float) -> float | None:
    """Richardson's relative economic value at cost-loss ratio ``ratio``.

    With the loss as 1 and the cost of acting as ``ratio``: (climatology's
    expense - the forecast's) / (climatology's - a perfect forecast's). None
    when the event always or never happened, where the three are equal.
    """
    n = hits + false_alarms + misses + rejections
    if n == 0:
        return None
    base_rate = (hits + misses) / n
    forecast = (hits + false_alarms) / n * ratio + misses / n
    climatology = min(ratio, base_rate)
    perfect = base_rate * ratio
    if climatology == perfect:
        return None
    return (climatology - forecast) / (climatology - perfect)


def economic_values(scores: Sequence[Mapping], rules: Rules) -> dict[float, float | None]:
    """The relative economic value at each of the bundle's cost-loss ratios."""
    return {ratio: economic_value(*contingency(scores, ratio), ratio) for ratio in rules.cost_loss_ratios}

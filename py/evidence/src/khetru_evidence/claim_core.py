"""The claim rule: one claim body per verdict band and issue date (R1, R2, R4, R5).

Pure; frozen at the bundle tag (KTD9). ``make_claim`` reads no file and knows no
``kind``: the live, exploratory and hindcast drivers all call it and add their
own ledger fields, so the same inputs give the same body on every path.

The probability is a fixed plotting position (KTD5): k members out of n have a
band-mean window total at or above the threshold, and the bundle names the
formula of k and n. The band mean and its coverage rule are ``bands.band_mean``
(KTD4); when the forecast covers less of the band than the coverage share, or
there is no forecast, the body is an abstain that says so.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from khetru_evidence import bands, forecasts
from khetru_evidence.ledger import timestamp
from khetru_evidence.semantics import Semantics, schedule

# Plotting positions a bundle may name. Each stays strictly between 0 and 1.
FORMULAS = {
    "(k + 0.5) / (n + 1)": lambda k, n: (k + 0.5) / (n + 1),
}
PROBABILITY_DECIMALS = 6

NO_COVERAGE = "no forecast coverage for this band"
CANT_TELL = "can't tell"

SOURCES = {"opendata": "ECMWF ENS open data", "tigge": "ECMWF ENS via TIGGE"}


class ClaimError(ValueError):
    """A claim cannot be made from these inputs as asked."""


@dataclass(frozen=True)
class Rule:
    """Claim rule values, read from a bundle's ``[claims]`` table."""

    name: str
    crop_stage: str
    threshold_mm: float
    probability_formula: str


def load_rule(toml_path: Path) -> Rule:
    with open(toml_path, "rb") as f:
        c = tomllib.load(f)["claims"]
    rule = Rule(
        name=str(c["rule"]),
        crop_stage=str(c["crop_stage"]),
        threshold_mm=float(c["threshold_mm"]),
        probability_formula=str(c["probability_formula"]),
    )
    if not rule.threshold_mm > 0:
        raise ClaimError("threshold_mm must be above 0")
    if rule.probability_formula not in FORMULAS:
        raise ClaimError(f"unknown probability formula {rule.probability_formula!r}")
    return rule


def probability(formula: str, k: int, n: int) -> float:
    """The stated probability when ``k`` of ``n`` members reach the threshold."""
    if formula not in FORMULAS:
        raise ClaimError(f"unknown probability formula {formula!r}")
    if not 0 <= k <= n or n < 1:
        raise ClaimError(f"k = {k} of n = {n} members is not a count")
    p = round(FORMULAS[formula](k, n), PROBABILITY_DECIMALS)
    if not 0 < p < 1:
        raise ClaimError(f"formula {formula!r} gave {p} for k = {k}, n = {n}; it must stay inside 0-1")
    return p


def make_claim(*, sem: Semantics, rule: Rule, coverage_share: float, band_map: bands.BandMap,
               verdict_band: str, issue_date: date, record: dict | None,
               evidence: Mapping[str, str]) -> dict:
    """The body of the claim, or of the abstain, for one verdict band on one issue date.

    ``record`` is the normalised forecast record of the issue date's run, or None
    when there is none. ``evidence`` maps each file the caller read (bundle, band
    map, record) to its SHA-256 and is copied into the body.
    """
    times = schedule(sem, issue_date)
    body = {
        "band": verdict_band,
        "issue_date": issue_date.isoformat(),
        "crop_stage": rule.crop_stage,
        "rule": rule.name,
        "threshold_mm": rule.threshold_mm,
        "window_start": timestamp(times.window_start),
        "window_end": timestamp(times.window_end),
        "expiry": timestamp(times.expiry),
        "evidence": dict(evidence),
    }
    if record is None:
        return _abstain(body, "no forecast record for the issue date's run")

    forecasts.validate_record(record)
    if forecasts.record_init(record) != times.run_init:
        raise ClaimError(f"record init {record['init']} is not the {issue_date} issue date's run")
    totals = forecasts.window_totals(record, times.step_start_hours, times.step_end_hours)
    mean = bands.band_mean(band_map, verdict_band, record["lats"], record["lons"], totals, coverage_share)
    coverage = round(float(mean.coverage.min()), 6)
    body |= {
        "source": SOURCES[record["source"]],
        "run_init": record["init"],
        "model_cycle": record["model_cycle"],
        "step_start_hours": times.step_start_hours,
        "step_end_hours": times.step_end_hours,
        "coverage": coverage,
    }
    if not mean.sufficient.all():
        return _abstain(body, f"the forecast covers {coverage:.0%} of the band, below the "
                              f"{coverage_share:.0%} coverage share")

    n = forecasts.member_count(record)
    k = int((mean.values >= rule.threshold_mm).sum())
    p = probability(rule.probability_formula, k, n)
    return body | {
        "type": "claim",
        "members": n,
        "members_at_or_above": k,
        "probability_formula": rule.probability_formula,
        "probability": p,
        "statement": statement(rule.threshold_mm, verdict_band, sem.rain_days, p),
    }


def statement(threshold_mm: float, verdict_band: str, rain_days: int, p: float) -> str:
    """The rain statement (R2): rain in a band over a window, never a farm outcome."""
    return f"≥{threshold_mm:g} mm rain in band {verdict_band} within {rain_days} days: {p:.0%}"


def _abstain(body: dict, detail: str) -> dict:
    return body | {
        "type": "abstain",
        "reason": NO_COVERAGE,
        "detail": detail,
        "statement": f"{CANT_TELL} — {NO_COVERAGE}",
    }

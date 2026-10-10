"""Scoring closed claims into the ledger, and reading the scores back (unfrozen, KTD9).

``score`` gives every closed slot of a claims file its score against one
observation vintage: it loads the bundle, the band map and the saved IMD
records, asks ``score_core.score_claim`` for the body, and appends it with the
driver's own fields. ``entry`` is the one place a body becomes a ledger entry;
the hindcast driver (U9) uses it too.

A claim and vintage are scored once. A later vintage appends a score that
supersedes the current one and says what changed (R9); the earlier score stays.
A slot waits, with nothing written, while its window is open, while the vintage
does not hold every date of the window, and while a real-time claim's timing is
pending. ``check`` re-derives every score from the files it names.

``standing`` reads a scores file back into what the pass/fail rule needs. It
refuses a bundle with no recorded tag, so development data never shows skill
against climatology (KTD11); ``outcomes`` is the count that may always be shown.
"""

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from khetru_evidence import bands, claims, obs_core, observations, score_core, semantics
from khetru_evidence import ledger as L

# Fields a driver or the ledger adds around a score body.
DRIVER_FIELDS = claims.DRIVER_FIELDS | {"type", "claim_id", "supersedes", "reason"}


def scores_file(claims_rel: str) -> str:
    """The scores file beside a claims file."""
    match PurePosixPath(claims_rel).parts:
        case ("claims", name):
            return f"scores/{name}"
        case ("hindcast", bundle, run, "claims.jsonl"):
            return f"hindcast/{bundle}/{run}/scores.jsonl"
    raise score_core.ScoreError(f"{claims_rel} is not a claims file")


def entry(body: dict, *, slot: Mapping, code: str, supersedes: str | None = None,
          reason: str | None = None) -> dict:
    """A score body as a ledger entry for ``slot``; ``code`` is the commit of the code that made it."""
    return L.score(ledger=slot["ledger"], claim_id=slot["id"], kind=slot["kind"], bundle=slot["bundle"],
                   run_id=slot.get("run_id"), code=code, supersedes=supersedes, reason=reason, **body)


def current(scores: Iterable[dict]) -> list[dict]:
    """Each claim's current score: the last one written for it."""
    heads = {e["claim_id"]: e for e in scores}
    return list(heads.values())


def outcomes(scores: Iterable[dict]) -> Counter[str]:
    return Counter(e["outcome"] for e in current(scores))


class _Observed:
    """The saved records of one vintage, and which file holds which date."""

    def __init__(self, root: Path, vintage: str):
        files = observations.record_files(root, vintage)
        if not files:
            raise obs_core.ObservationError(f"no saved records of vintage {vintage}")
        records = [obs_core.decode_record(p.read_bytes()) for p in files]
        self.files = files
        self.series = obs_core.daily(records)
        self._dates = {p: set(obs_core.record_dates(r)) for p, r in zip(files, records)}
        self._bands: dict[tuple[str, float], dict[date, float]] = {}

    def holds(self, dates: Iterable[date]) -> bool:
        return set(dates) <= set(self.series.dates)

    def files_holding(self, dates: Iterable[date]) -> list[Path]:
        return [p for p in self.files if self._dates[p] & set(dates)]

    def band(self, band_map: bands.BandMap, verdict_band: str, coverage_share: float) -> dict[date, float]:
        key = (verdict_band, coverage_share)
        if key not in self._bands:
            self._bands[key] = obs_core.band_daily(self.series, band_map, verdict_band, coverage_share)
        return self._bands[key]


class _Scorer:
    """What one scoring pass reads: bundles, the band map and observation vintages, each loaded once."""

    def __init__(self, ledger: L.Ledger, attested: Mapping[str, datetime] | None):
        self.root = ledger.root
        self.band_map = bands.decode((self.root / bands.BAND_MAP_PATH).read_bytes())
        self.attested = attested or {}
        self._bundles: dict[str, tuple[claims.Bundle, score_core.Rules, Path]] = {}
        self._observed: dict[str, _Observed] = {}

    def bundle(self, name: str) -> tuple[claims.Bundle, score_core.Rules, Path]:
        if name not in self._bundles:
            b = claims.Bundle(self.root, name)
            toml = b.holding("scoring")
            self._bundles[name] = (b, score_core.load_rules(toml), toml)
        return self._bundles[name]

    def observed(self, vintage: str) -> _Observed:
        if vintage not in self._observed:
            self._observed[vintage] = _Observed(self.root, vintage)
        return self._observed[vintage]

    def window(self, slot: Mapping) -> tuple[semantics.Schedule, tuple[date, ...]]:
        times = semantics.schedule(self.bundle(slot["bundle"])[0].sem, date.fromisoformat(slot["issue_date"]))
        return times, obs_core.imd_dates(times)

    def timing(self, slot: Mapping) -> tuple[str | None, str | None]:
        """(timing test, result) for a claim; (None, None) for an abstain or a ``not_issued`` slot."""
        if slot["type"] != "claim":
            return None, None
        # A live claim is written in real time, so only a proof can say when (KTD3).
        test = score_core.OPENTIMESTAMPS if slot["kind"] == "live" else score_core.FORECAST_AVAILABILITY
        sem = self.bundle(slot["bundle"])[0].sem
        return test, score_core.timing(sem, slot, test, self.attested.get(slot["id"]))

    def body(self, slot: Mapping, voided: bool, vintage: str) -> dict | None:
        """The score body of ``slot`` against ``vintage``; None while its timing is pending."""
        test, result = self.timing(slot)
        if result == score_core.PENDING:
            return None
        bundle, rules, scoring_toml = self.bundle(slot["bundle"])
        _, dates = self.window(slot)
        observed, final = self.observed(vintage), self.observed(rules.climatology_vintage)
        share, obs = bundle.coverage_share, bundle.observations
        year = date.fromisoformat(slot["issue_date"]).year
        years = [y for y in observations.full_years(final.series)
                 if y >= obs.climatology_first_year and y != year]
        climatology = obs_core.climatology(
            final.band(self.band_map, slot["band"], share), dates[0], years=years, days=len(dates),
            threshold_mm=bundle.rule.threshold_mm, half_window_days=obs.climatology_half_window_days,
            pseudo_count=obs.climatology_pseudo_count,
        )
        paths = {*bundle.files, scoring_toml, self.root / bands.BAND_MAP_PATH,
                 *observed.files_holding(dates), *final.files}
        return score_core.score_claim(
            sem=bundle.sem, threshold_mm=bundle.rule.threshold_mm, slot=slot, voided=voided,
            band=observed.band(self.band_map, slot["band"], share), vintage=vintage, climatology=climatology,
            timing_test=test, timing_result=result,
            evidence={p.relative_to(self.root).as_posix(): claims.sha256(p) for p in sorted(paths)},
        )


def _slots(ledger: L.Ledger, claims_rel: str) -> tuple[list[dict], set[str]]:
    """The slot entries of a claims file, and the IDs of the voided ones."""
    entries = ledger.read(claims_rel)
    voided = {e["target_id"] for e in entries if e["type"] == "correction" and e["action"] == "void"}
    return [e for e in entries if e["type"] in L.SLOT_TYPES], voided


def _replaces(vintage: str, earlier: str) -> bool:
    """Final observations replace real-time ones, and a later retrieval an earlier one; never the reverse."""
    def rank(name: str) -> tuple[bool, str]:
        product, _, retrieved = name.partition("-r")
        return product == "final", retrieved

    return rank(vintage) > rank(earlier)


def score(ledger: L.Ledger, claims_rel: str, *, vintage: str, code: str,
          attested: Mapping[str, datetime] | None = None) -> list[dict]:
    """Append a score against ``vintage`` for every closed slot that can take one; returns what was written.

    ``attested`` maps a real-time claim's ID to the time its OpenTimestamps
    proof attests; a live claim with none waits.
    """
    scorer = _Scorer(ledger, attested)
    observed = scorer.observed(vintage)
    scores_rel = scores_file(claims_rel)
    slots, voided = _slots(ledger, claims_rel)
    earlier = ledger.read(scores_rel)
    scored = {e["id"] for e in earlier}
    heads = {e["claim_id"]: e for e in earlier}
    now = ledger.clock()
    written = []
    for slot in slots:
        times, dates = scorer.window(slot)
        head = heads.get(slot["id"])
        if (now < times.window_end or not observed.holds(dates) or L.score_id(slot["id"], vintage) in scored
                or (head is not None and not _replaces(vintage, head["vintage"]))):
            continue
        body = scorer.body(slot, slot["id"] in voided, vintage)
        if body is None:
            continue
        replacing = {}
        if head is not None:
            change = (f"outcome {head['outcome']} -> {body['outcome']}" if head["outcome"] != body["outcome"]
                      else f"outcome still {body['outcome']}")
            replacing = {"supersedes": head["id"],
                         "reason": f"{vintage} observations replace {head['vintage']}: {change}"}
        written.append(ledger.append(scores_rel, entry(body, slot=slot, code=code, **replacing)))
    return written


def check(ledger: L.Ledger, claims_rel: str, attested: Mapping[str, datetime] | None = None) -> list[str]:
    """Every score beside ``claims_rel`` that the files it names no longer reproduce."""
    scorer = _Scorer(ledger, attested)
    slots, voided = _slots(ledger, claims_rel)
    by_id = {slot["id"]: slot for slot in slots}
    problems = []
    for e in ledger.read(scores_file(claims_rel)):
        slot = by_id.get(e["claim_id"])
        if slot is None:
            problems.append(f"{e['id']}: no claim {e['claim_id']} in {claims_rel}")
            continue
        changed = claims.changed_evidence(ledger.root, e)
        if changed:
            problems.append(f"{e['id']}: evidence file {changed} is missing or changed")
            continue
        stored = {k: v for k, v in e.items() if k not in DRIVER_FIELDS}
        if scorer.body(slot, slot["id"] in voided, e["vintage"]) != stored:
            problems.append(f"{e['id']}: does not re-derive from its evidence files")
    return problems


# --- reading scores back -----------------------------------------------------


@dataclass(frozen=True)
class Standing:
    """What the pass/fail rule reads from one bundle's current scores in one file."""

    outcomes: Counter[str]
    skill: score_core.Skill
    episodes: dict[str, tuple[int, int]]  # verdict band -> (positive, negative) independent episodes
    economic_values: dict[str, dict[float, float | None]]  # verdict band -> cost-loss ratio -> value


def is_tagged(ledger: L.Ledger, bundle: str) -> bool:
    return any(t["name"] == f"{ledger.name}/bundle-{bundle}" for t in ledger.tags())


def standing(ledger: L.Ledger, scores_rel: str, bundle: str) -> Standing:
    """Skill, episode counts and cost-loss values of ``bundle``'s current scores in ``scores_rel``."""
    if not is_tagged(ledger, bundle):
        raise score_core.ScoreError(
            f"bundle {bundle} has no recorded tag; skill against climatology is held back until it has (KTD11)")
    scorer = _Scorer(ledger, None)
    b, rules, _ = scorer.bundle(bundle)
    scores = [e for e in current(ledger.read(scores_rel)) if e["bundle"] == bundle]
    hindcast = any(e["kind"] == "hindcast" for e in scores)
    by_band: dict[str, list[dict]] = {}
    for e in scores:
        by_band.setdefault(e["band"], []).append(e)
    return Standing(
        outcomes=outcomes(scores),
        skill=score_core.skill(score_core.rows(scores), score_core.season if hindcast else score_core.issue_date,
                               rules),
        episodes={band: _episodes(scorer, own, b) for band, own in sorted(by_band.items())},
        economic_values={band: score_core.economic_values(own, rules) for band, own in sorted(by_band.items())},
    )


def _episodes(scorer: _Scorer, scores: list[dict], bundle: claims.Bundle) -> tuple[int, int]:
    """(positive, negative) independent episodes of one band, from the daily values its scores hold."""
    seasons: dict[int, tuple[dict[date, float], list[tuple[date, ...]]]] = {}
    for e in sorted(scores, key=lambda e: e["issue_date"]):
        _, dates = scorer.window(e)
        band, windows = seasons.setdefault(dates[0].year, ({}, []))
        band |= {d: math.nan if v is None else v for d, v in zip(dates, e["observed_daily_mm"])}
        windows.append(dates)
    obs = bundle.observations
    counts = [obs_core.episode_counts(band, windows, bundle.rule.threshold_mm, obs.wet_day_mm, obs.dry_day_gap_days)
              for band, windows in seasons.values()]
    return sum(p for p, _ in counts), sum(n for _, n in counts)

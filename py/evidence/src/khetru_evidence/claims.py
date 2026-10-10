"""Issuing claims into the ledger: the live and exploratory driver (unfrozen, KTD9).

``issue`` loads the bundle, the band map and the issue date's saved forecast
record, asks ``claim_core.make_claim`` for each verdict band's body, and appends
it with the driver's own fields (``kind``, ``bundle``, ``code``). ``entry`` is
the one place a body becomes a ledger entry; the hindcast driver (U9) uses it
too.

A slot is written once: issuing a date again writes nothing. ``backfill_missing``
records ``not_issued`` for slots whose window started with no claim (R10a); it
never writes a claim. ``check`` re-derives every claim from the files it names.
"""

import hashlib
import tomllib
from datetime import date
from pathlib import Path

from khetru_evidence import bands, claim_core, forecasts, obs_core, semantics
from khetru_evidence import ledger as L

BUNDLES_DIR = "bundles"
# Fields a driver or the ledger adds around a claim body.
DRIVER_FIELDS = frozenset({"id", "ledger", "kind", "bundle", "run_id", "code", "recorded_at"})


class Bundle:
    """A bundle's rule values and the files they came from."""

    def __init__(self, ledger_root: Path, name: str):
        self.name = name
        directory = Path(ledger_root) / BUNDLES_DIR / name
        self._tomls = sorted(directory.glob("*.toml"))
        if not self._tomls:
            raise claim_core.ClaimError(f"no bundle {name!r} under {directory.parent}")
        sem, rule, obs = (self._holding(table) for table in ("semantics", "claims", "observations"))
        self.sem = semantics.load(sem)
        self.rule = claim_core.load_rule(rule)
        self.coverage_share = obs_core.load_rules(obs).coverage_share
        # Only the files a claim is made from are its evidence, so a file added
        # to the bundle later leaves earlier claims re-derivable.
        self.files = sorted({sem, rule, obs})

    def _holding(self, table: str) -> Path:
        """The bundle file with ``table``; a bundle may keep its tables in one file or several."""
        found = [p for p in self._tomls if table in tomllib.loads(p.read_text())]
        if len(found) != 1:
            raise claim_core.ClaimError(f"bundle {self.name} must hold exactly one [{table}] table")
        return found[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kind_file(kind: str) -> str:
    return f"claims/{kind}.jsonl"


def entry(body: dict, *, ledger: str, kind: str, bundle: str, code: str, run_id: str | None = None) -> dict:
    """A claim body as a ledger entry of ``kind``; ``code`` is the commit of the code that made it."""
    fields = {k: v for k, v in body.items() if k != "type"}
    make = {"claim": L.claim, "abstain": L.abstain}[body["type"]]
    return make(ledger=ledger, kind=kind, bundle=bundle, run_id=run_id, code=code, **fields)


def bodies(ledger_root: Path, bundle: Bundle, issue_date: date, saved: Path | None) -> list[dict]:
    """Each verdict band's claim body for ``issue_date``, from the record at ``saved`` if there is one."""
    root = Path(ledger_root)
    paths = [*bundle.files, root / bands.BAND_MAP_PATH]
    band_map = bands.decode((root / bands.BAND_MAP_PATH).read_bytes())
    record = None
    if saved is not None:
        record = forecasts.decode_record(saved.read_bytes())
        paths.append(saved)
    evidence = {p.relative_to(root).as_posix(): _sha256(p) for p in paths}
    return [
        claim_core.make_claim(
            sem=bundle.sem, rule=bundle.rule, coverage_share=bundle.coverage_share, band_map=band_map,
            verdict_band=band, issue_date=issue_date, record=record, evidence=evidence,
        )
        for band in band_map.verdict_bands
    ]


def issue(ledger: L.Ledger, issue_date: date, *, bundle: str, kind: str, code: str,
          source: str = "opendata", abstain_if_missing: bool = False) -> list[dict]:
    """Append ``issue_date``'s claim for every verdict band that has none; returns what was written.

    Refuses before the run can be available, and refuses a ``live`` claim once
    its window has started. With no saved record it refuses too, unless
    ``abstain_if_missing`` says the source is known to be unavailable (R5).
    """
    b = Bundle(ledger.root, bundle)
    times = semantics.schedule(b.sem, issue_date)
    now = ledger.clock()
    if now < times.run_available:
        raise claim_core.ClaimError(
            f"the {issue_date} run is not available before {L.timestamp(times.run_available)}")
    if kind == "live" and now >= times.late_cutoff:
        raise claim_core.ClaimError(
            f"the {issue_date} window started at {L.timestamp(times.late_cutoff)}; "
            "a live claim is never back-filled (R10a)")
    rel = kind_file(kind)
    taken = {e["id"] for e in ledger.read(rel)}
    band_map = bands.decode((ledger.root / bands.BAND_MAP_PATH).read_bytes())
    if all(L.slot_id(ledger.name, kind, bundle, band, issue_date.isoformat()) in taken
           for band in band_map.verdict_bands):
        return []
    saved = ledger.root / forecasts.record_path(source, times.run_init)
    if not saved.exists():
        if not abstain_if_missing:
            raise claim_core.ClaimError(
                f"no saved {source} record for the {issue_date} run; archive it first, "
                "or pass --abstain-if-missing to record an abstain")
        saved = None
    written = []
    for body in bodies(ledger.root, b, issue_date, saved):
        new = entry(body, ledger=ledger.name, kind=kind, bundle=bundle, code=code)
        if new["id"] not in taken:
            written.append(ledger.append(rel, new))
    return written


def backfill_missing(ledger: L.Ledger, year: int, *, bundle: str, kind: str) -> list[dict]:
    """Append ``not_issued`` for every slot of ``year`` whose window started with no entry (R10a)."""
    b = Bundle(ledger.root, bundle)
    band_map = bands.decode((ledger.root / bands.BAND_MAP_PATH).read_bytes())
    rel = kind_file(kind)
    taken = {e["id"] for e in ledger.read(rel)}
    now = ledger.clock()
    written = []
    for issue_date in semantics.issue_dates(b.sem, year):
        if now < semantics.schedule(b.sem, issue_date).window_start:
            continue
        for band in band_map.verdict_bands:
            new = L.not_issued(ledger=ledger.name, kind=kind, bundle=bundle, band=band, issue_date=issue_date)
            if new["id"] not in taken:
                written.append(ledger.append(rel, new))
    return written


def check(ledger: L.Ledger, rel: str) -> list[str]:
    """Every claim or abstain in ``rel`` that the files it names no longer reproduce."""
    problems = []
    for e in ledger.read(rel):
        if e["type"] not in ("claim", "abstain"):
            continue
        changed = [name for name, sha in e["evidence"].items()
                   if not (ledger.root / name).exists() or _sha256(ledger.root / name) != sha]
        if changed:
            problems.append(f"{e['id']}: evidence file {changed[0]} is missing or changed")
            continue
        bundle = Bundle(ledger.root, e["bundle"])
        inputs = [ledger.root / name for name in e["evidence"] if name.startswith(f"{forecasts.INPUTS_DIR}/")]
        saved = inputs[0] if inputs else None
        again = {b["band"]: b for b in bodies(ledger.root, bundle, date.fromisoformat(e["issue_date"]), saved)}
        stored = {k: v for k, v in e.items() if k not in DRIVER_FIELDS}
        if again.get(e["band"]) != stored:
            problems.append(f"{e['id']}: does not re-derive from its evidence files")
    return problems

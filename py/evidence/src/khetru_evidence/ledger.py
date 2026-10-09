"""Append-only claim ledger: canonical JSONL, natural-key IDs, invariants, manifests (KTD6).

A ledger lives in ``ledger/<name>/``. Its JSONL files only ever grow, one canonical
entry per line. Each file has a fixed role:

- ``claims/{live,exploratory}.jsonl``: claims, abstains, ``not_issued``, corrections
- ``scores/{live,exploratory}.jsonl``: scores and re-scores
- ``hindcast/<bundle>/runs.jsonl``: hindcast run lifecycle
- ``hindcast/<bundle>/<run>/{claims,scores}.jsonl``: one hindcast run's entries
- ``tags.jsonl``: bundle tags and the commit each one pointed at when recorded

Entries are plain dicts typed by their ``type`` field; build them with the
constructors below so their ``id`` comes from the natural key.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

KINDS = ("live", "hindcast", "exploratory")
OUTCOMES = ("held", "not_held", "unverifiable")
RUN_STATUSES = ("started", "aborted", "recorded", "invalidated")
SLOT_TYPES = ("claim", "abstain", "not_issued")
CORRECTION_ACTIONS = ("void", "fix")
WRITE_ONCE_DIRS = ("inputs", "observations", "bands", "bundles", "stamps")
MANIFEST_DIR = "manifests"
DEV_BUNDLE = "dev"
# Claim fields a correction may fix. Everything else can affect scoring, so a
# mistake there is handled by voiding the claim instead.
FIXABLE_FIELDS = frozenset({"note"})

_KEY_FIELDS = ("kind", "bundle")
_REQUIRED = {
    "claim": (*_KEY_FIELDS, "band", "issue_date"),
    "abstain": (*_KEY_FIELDS, "band", "issue_date", "reason"),
    "not_issued": (*_KEY_FIELDS, "band", "issue_date"),
    "score": (*_KEY_FIELDS, "claim_id", "vintage", "outcome"),
    "correction": (*_KEY_FIELDS, "target_id", "seq", "action", "reason"),
    "run": (*_KEY_FIELDS, "run_id", "seq", "status"),
    "tag": ("name", "commit"),
}
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_ID_SEPARATORS = (":", "@", "#")

Clock = Callable[[], datetime]


class LedgerError(ValueError):
    """An entry or file breaks a ledger invariant; nothing was written."""


def system_clock() -> datetime:
    return datetime.now(UTC)


# --- encoding -------------------------------------------------------------


def encode(entry: dict) -> bytes:
    """Canonical bytes: sorted keys, compact separators, UTF-8, trailing newline."""
    text = json.dumps(
        entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8") + b"\n"


def decode(line: bytes) -> dict:
    """Parse one line (without its newline) and insist it is canonical."""
    try:
        entry = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LedgerError(f"line does not parse as JSON: {e}") from None
    if not isinstance(entry, dict):
        raise LedgerError("line is not a JSON object")
    try:
        canonical = encode(entry)
    except ValueError:
        canonical = None
    if canonical != line + b"\n":
        raise LedgerError("line is not canonical JSON (sorted keys, compact separators)")
    return entry


def timestamp(moment: datetime) -> str:
    if moment.utcoffset() != timedelta(0):
        raise LedgerError("clock must return an aware UTC datetime")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_lines(data: bytes) -> list[bytes]:
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    return lines


# --- natural-key IDs and constructors -------------------------------------


def slot_id(ledger, kind, bundle, band, issue_date, run_id=None) -> str:
    """ID shared by a claim, abstain or ``not_issued`` for one issue slot.

    Hindcast slots also carry their run, so each run has its own key space.
    """
    parts = [ledger, kind, bundle, *([run_id] if run_id is not None else []), band, issue_date]
    for part in parts:
        if not isinstance(part, str) or not part or any(s in part for s in _ID_SEPARATORS):
            raise LedgerError(f"key component {part!r} must be a non-empty string without : @ #")
    return ":".join(parts)


def score_id(claim_id: str, vintage: str) -> str:
    return f"{claim_id}@{vintage}"


def run_id(bundle: str, seq: int) -> str:
    return f"{bundle}-run-{seq}"


def _slot(type_, *, ledger, kind, bundle, band, issue_date, run_id=None, **payload) -> dict:
    if isinstance(issue_date, date):
        issue_date = issue_date.isoformat()
    entry = {
        **payload,
        "type": type_,
        "ledger": ledger,
        "kind": kind,
        "bundle": bundle,
        "band": band,
        "issue_date": issue_date,
    }
    if run_id is not None:
        entry["run_id"] = run_id
    entry["id"] = slot_id(ledger, kind, bundle, band, issue_date, run_id)
    return entry


def claim(**fields) -> dict:
    return _slot("claim", **fields)


def abstain(*, reason: str, **fields) -> dict:
    return _slot("abstain", reason=reason, **fields)


def not_issued(**fields) -> dict:
    return _slot("not_issued", **fields)


def score(*, ledger, claim_id, kind, bundle, vintage, outcome,
          supersedes=None, reason=None, run_id=None, **payload) -> dict:
    entry = {
        **payload,
        "type": "score",
        "ledger": ledger,
        "claim_id": claim_id,
        "kind": kind,
        "bundle": bundle,
        "vintage": vintage,
        "outcome": outcome,
        "id": score_id(claim_id, vintage),
    }
    for name, value in (("supersedes", supersedes), ("reason", reason), ("run_id", run_id)):
        if value is not None:
            entry[name] = value
    return entry


def correction(*, ledger, target_id, kind, bundle, seq, action, reason,
               field=None, value=None, run_id=None) -> dict:
    entry = {
        "type": "correction",
        "ledger": ledger,
        "target_id": target_id,
        "kind": kind,
        "bundle": bundle,
        "seq": seq,
        "action": action,
        "reason": reason,
        "id": correction_id(target_id, seq),
    }
    if action == "fix":
        entry["field"] = field
        entry["value"] = value
    if run_id is not None:
        entry["run_id"] = run_id
    return entry


def run(*, ledger, bundle, seq, status, **payload) -> dict:
    rid = run_id(bundle, seq)
    return {
        **payload,
        "type": "run",
        "ledger": ledger,
        "kind": "hindcast",
        "bundle": bundle,
        "seq": seq,
        "run_id": rid,
        "status": status,
        "id": run_status_id(bundle, seq, status),
    }


def tag(*, ledger, name, commit) -> dict:
    return {"type": "tag", "ledger": ledger, "name": name, "commit": commit, "id": tag_id(name)}


def correction_id(target_id: str, seq: int) -> str:
    return f"{target_id}#{seq}"


def run_status_id(bundle: str, seq: int, status: str) -> str:
    return f"{run_id(bundle, seq)}:{status}"


def tag_id(name: str) -> str:
    return f"tag:{name}"


def _expected_id(e: dict) -> str:
    match e["type"]:
        case "claim" | "abstain" | "not_issued":
            return slot_id(e["ledger"], e["kind"], e["bundle"], e["band"], e["issue_date"], e.get("run_id"))
        case "score":
            return score_id(e["claim_id"], e["vintage"])
        case "correction":
            return correction_id(e["target_id"], e["seq"])
        case "run":
            return run_status_id(e["bundle"], e["seq"], e["status"])
        case "tag":
            return tag_id(e["name"])


# --- schema ---------------------------------------------------------------


def validate(e: dict) -> None:
    """Check one entry against its type's schema, independent of any file."""
    type_ = e.get("type")
    if type_ not in _REQUIRED:
        raise LedgerError(f"unknown entry type {type_!r}")
    required = ("id", "ledger", "recorded_at", *_REQUIRED[type_])
    missing = [f for f in required if f not in e]
    if missing:
        raise LedgerError(f"{type_} is missing field(s): {', '.join(missing)}")
    for f in required:
        if f == "seq":
            if not isinstance(e[f], int) or isinstance(e[f], bool) or e[f] < 1:
                raise LedgerError("seq must be a positive integer")
        elif not isinstance(e[f], str) or not e[f]:
            raise LedgerError(f"{f} must be a non-empty string")
    if not _TIMESTAMP.fullmatch(e["recorded_at"]):
        raise LedgerError("recorded_at must be UTC like 2026-10-19T10:30:00Z")

    if type_ == "tag":
        if not _SHA1.fullmatch(e["commit"]):
            raise LedgerError("tag commit must be a full 40-hex SHA")
    else:
        _validate_kind(e)

    if type_ in SLOT_TYPES:
        try:
            date.fromisoformat(e["issue_date"])
        except ValueError:
            raise LedgerError("issue_date must be an ISO date") from None
    elif type_ == "score":
        if e["outcome"] not in OUTCOMES:
            raise LedgerError(f"outcome must be one of {', '.join(OUTCOMES)}")
        if "supersedes" in e and not (isinstance(e.get("reason"), str) and e["reason"]):
            raise LedgerError("a re-score needs a reason")
    elif type_ == "correction":
        if e["action"] not in CORRECTION_ACTIONS:
            raise LedgerError(f"correction action must be one of {', '.join(CORRECTION_ACTIONS)}")
        if e["action"] == "fix" and not (isinstance(e.get("field"), str) and "value" in e):
            raise LedgerError("a fix needs field and value")
    elif type_ == "run":
        if e["status"] not in RUN_STATUSES:
            raise LedgerError(f"run status must be one of {', '.join(RUN_STATUSES)}")

    if e["id"] != _expected_id(e):
        raise LedgerError(f"id {e['id']!r} does not match its natural key {_expected_id(e)!r}")


def _validate_kind(e: dict) -> None:
    if e["kind"] not in KINDS:
        raise LedgerError(f"kind must be one of {', '.join(KINDS)}")
    if e["bundle"] == DEV_BUNDLE and e["kind"] != "exploratory":
        raise LedgerError("dev bundle entries are always exploratory")
    if e["kind"] == "hindcast" and not isinstance(e.get("run_id"), str):
        raise LedgerError("hindcast entries need a run_id")


# --- file placement and per-file invariants -------------------------------


@dataclass(frozen=True)
class _FileRule:
    types: tuple[str, ...]
    kind: str | None = None
    bundle: str | None = None
    runs_file: str | None = None  # hindcast entries must name a run started here
    run_id: str | None = None  # the run directory a hindcast entry must belong to


def _file_rule(rel: str) -> _FileRule | None:
    claims = (*SLOT_TYPES, "correction")
    match PurePosixPath(rel).parts:
        case ("claims", "live.jsonl" | "exploratory.jsonl" as name):
            return _FileRule(claims, kind=name.removesuffix(".jsonl"))
        case ("scores", "live.jsonl" | "exploratory.jsonl" as name):
            return _FileRule(("score",), kind=name.removesuffix(".jsonl"))
        case ("hindcast", bundle, "runs.jsonl"):
            return _FileRule(("run",), kind="hindcast", bundle=bundle)
        case ("hindcast", bundle, run, "claims.jsonl" | "scores.jsonl" as name):
            types = claims if name == "claims.jsonl" else ("score",)
            runs = f"hindcast/{bundle}/runs.jsonl"
            return _FileRule(types, kind="hindcast", bundle=bundle, runs_file=runs, run_id=run)
        case ("tags.jsonl",):
            return _FileRule(("tag",))
    return None


class _FileState:
    """Invariants that span the lines of one file, checked one entry at a time."""

    def __init__(self, ledger: "Ledger", rel: str):
        rule = _file_rule(rel)
        if rule is None:
            raise LedgerError(f"{rel} is not a recognised ledger file")
        self.ledger, self.rel, self.rule = ledger, rel, rule
        self.types: dict[str, str] = {}  # id -> entry type
        self.score_heads: dict[str, str] = {}  # claim id -> current score id
        self.corrections: Counter[str] = Counter()
        self.voided: set[str] = set()
        self._started: set[str] | None = None

    def admit(self, e: dict) -> None:
        validate(e)
        self._check_placement(e)
        eid = e["id"]
        if eid in self.types:
            if e["type"] == "not_issued" and self.types[eid] in ("claim", "abstain"):
                raise LedgerError(f"not_issued refused: {eid} already has a {self.types[eid]}")
            raise LedgerError(f"duplicate natural key {eid}")
        if e["type"] == "score":
            self._check_supersede(e)
        elif e["type"] == "correction":
            self._check_correction(e)
        self.types[eid] = e["type"]

    def _check_placement(self, e: dict) -> None:
        rule, rel = self.rule, self.rel
        if e["type"] not in rule.types:
            raise LedgerError(f"{rel} cannot hold {e['type']} entries")
        if e["ledger"] != self.ledger.name:
            raise LedgerError(f"entry belongs to ledger {e['ledger']!r}, not {self.ledger.name!r}")
        if rule.kind and e["kind"] != rule.kind:
            raise LedgerError(f"{rel} only holds {rule.kind} entries, not {e['kind']}")
        if rule.bundle and e["bundle"] != rule.bundle:
            raise LedgerError(f"{rel} only holds bundle {rule.bundle} entries, not {e['bundle']}")
        if rule.run_id and e["run_id"] != rule.run_id:
            raise LedgerError(f"{rel} only holds run {rule.run_id} entries, not {e['run_id']}")
        if rule.runs_file and e["run_id"] not in self._started_runs():
            raise LedgerError(f"run {e['run_id']} has no started entry in {rule.runs_file}")

    def _started_runs(self) -> set[str]:
        if self._started is None:
            entries = self.ledger._read_lenient(self.rule.runs_file)
            self._started = {e.get("run_id") for e in entries if e.get("status") == "started"}
        return self._started

    def _check_supersede(self, e: dict) -> None:
        head = self.score_heads.get(e["claim_id"])
        sup = e.get("supersedes")
        if sup != head:
            if head is None:
                raise LedgerError(f"supersedes {sup}, but claim {e['claim_id']} has no current score")
            raise LedgerError(
                f"claim {e['claim_id']} already has current score {head}; a re-score must supersede it"
            )
        self.score_heads[e["claim_id"]] = e["id"]

    def _check_correction(self, e: dict) -> None:
        target = e["target_id"]
        if self.types.get(target) not in SLOT_TYPES:
            raise LedgerError(f"correction targets unknown entry {target}")
        if e["seq"] != self.corrections[target] + 1:
            raise LedgerError(f"correction sequence for {target} must be {self.corrections[target] + 1}")
        if target in self.voided:
            raise LedgerError(f"{target} is already voided")
        if e["action"] == "fix" and e["field"] not in FIXABLE_FIELDS:
            raise LedgerError(f"field {e['field']!r} affects scoring; void the claim instead")
        self.corrections[target] += 1
        if e["action"] == "void":
            self.voided.add(target)


# --- the ledger -----------------------------------------------------------


class Ledger:
    def __init__(self, root: Path, clock: Clock = system_clock):
        self.root = Path(root)
        self.name = self.root.name
        self.clock = clock

    def read(self, rel: str) -> list[dict]:
        path = self.root / rel
        if not path.exists():
            return []
        return [decode(line) for line in _split_lines(path.read_bytes())]

    def append(self, rel: str, entry: dict) -> dict:
        """Validate ``entry`` against the whole file, then write it in one write.

        Returns the entry as written (with ``recorded_at``). Refuses, leaving the
        file untouched, on any broken invariant.
        """
        path = self.root / rel
        data = path.read_bytes() if path.exists() else b""
        if data and not data.endswith(b"\n"):
            raise LedgerError(f"{rel} lacks a trailing newline; refusing to append")
        state = _FileState(self, rel)
        for n, line in enumerate(_split_lines(data), 1):
            try:
                state.admit(decode(line))
            except LedgerError as e:
                raise LedgerError(f"{rel}:{n}: {e}") from None
        entry = {**entry, "recorded_at": timestamp(self.clock())}
        state.admit(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            f.write(encode(entry))
        return entry

    def check_file(self, rel: str) -> list[tuple[int, str]]:
        """Every problem in one ledger file as (line number, message); line 0 = whole file."""
        data = (self.root / rel).read_bytes()
        try:
            state = _FileState(self, rel)
        except LedgerError as e:
            return [(0, str(e))]
        problems = []
        lines = _split_lines(data)
        if data and not data.endswith(b"\n"):
            problems.append((len(lines), "missing trailing newline"))
        for n, line in enumerate(lines, 1):
            try:
                state.admit(decode(line))
            except LedgerError as e:
                problems.append((n, str(e)))
        return problems

    def ledger_files(self) -> list[str]:
        """Relative paths of every JSONL ledger file (evidence and manifests excluded)."""
        skip = (*WRITE_ONCE_DIRS, MANIFEST_DIR)
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*.jsonl")
            if p.is_file() and p.relative_to(self.root).parts[0] not in skip
        )

    def tags(self) -> list[dict]:
        return [e for e in self._read_lenient("tags.jsonl") if e.get("type") == "tag"]

    def _read_lenient(self, rel: str) -> list[dict]:
        """Parseable entries of a file, skipping bad lines (``check_file`` reports those)."""
        path = self.root / rel
        if not path.exists():
            return []
        entries = []
        for line in _split_lines(path.read_bytes()):
            try:
                entries.append(json.loads(line))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return [e for e in entries if isinstance(e, dict)]

    # --- manifests ---

    def write_manifest(self) -> str:
        """Record every ledger file's length and SHA-256; returns the manifest's path."""
        created_at = timestamp(self.clock())
        rel = f"{MANIFEST_DIR}/{created_at.replace(':', '')}.json"
        path = self.root / rel
        if path.exists():
            raise LedgerError(f"manifest {rel} already exists")
        files = {}
        for name in self.ledger_files():
            data = (self.root / name).read_bytes()
            files[name] = {"length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as f:
            f.write(encode({"created_at": created_at, "files": files}))
        return rel

    def manifests(self) -> list[str]:
        return sorted(
            p.relative_to(self.root).as_posix() for p in (self.root / MANIFEST_DIR).glob("*.json")
        )

    def check_manifest(self, rel: str) -> list[tuple[str, str]]:
        """Problems as (ledger file, message) where a current file fails to extend the manifest."""
        try:
            files = json.loads((self.root / rel).read_bytes())["files"]
        except (ValueError, KeyError, TypeError) as e:
            return [(rel, f"unreadable manifest: {e}")]
        problems = []
        for name, record in sorted(files.items()):
            path = self.root / name
            if not path.exists():
                problems.append((name, "is missing"))
                continue
            data = path.read_bytes()
            length = record["length"]
            if len(data) < length:
                problems.append((name, f"is shorter than the manifest ({len(data)} < {length} bytes)"))
            elif hashlib.sha256(data[:length]).hexdigest() != record["sha256"]:
                problems.append((name, f"does not extend the manifest (first {length} bytes differ)"))
        return problems

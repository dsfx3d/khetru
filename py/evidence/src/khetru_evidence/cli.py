"""`evidence` command line. Subcommands are added per unit: `verify` (U2), `bands` (U3), `observe` (U4), `archive` and `same-record` (U5), `issue` (U6), `score` (U7), `bundle` (U8)."""

import argparse
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from khetru_evidence.ledger import DEV_BUNDLE, MANIFEST_DIR, OUTCOMES, WRITE_ONCE_DIRS, Ledger

LEDGERS_DIR = "ledger"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evidence", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    verify_cmd = commands.add_parser(
        "verify", help="check that the ledger only grew since a base ref and holds its invariants"
    )
    verify_cmd.add_argument("--base", required=True, help="git ref or tree to compare against")
    verify_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    verify_cmd.set_defaults(run=_run_verify)

    archive_cmd = commands.add_parser(
        "archive", help="save a day's ECMWF open-data ENS runs (1 Oct-30 Nov) as write-once inputs"
    )
    archive_cmd.add_argument(
        "--date", type=date.fromisoformat, help="run date YYYY-MM-DD (default: today, UTC)"
    )
    archive_cmd.add_argument(
        "--time", type=int, choices=(0, 12), action="append", dest="hours",
        help="run hour UTC; repeat for both (default: 0 and 12)",
    )
    archive_cmd.add_argument(
        "--source", default="ecmwf", choices=("ecmwf", "aws", "azure", "google"),
        help="open-data mirror (default: ecmwf)",
    )
    archive_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    archive_cmd.set_defaults(run=_run_archive)

    same_cmd = commands.add_parser(
        "same-record", help="exit 0 if two saved records hold the same run, whichever mirror served it"
    )
    same_cmd.add_argument("records", type=Path, nargs=2, metavar="RECORD")
    same_cmd.set_defaults(run=_run_same_record)

    bands_cmd = commands.add_parser(
        "bands", help="build: make the band map from its recorded sources, or check a rebuild "
        "reproduces it; zones: print the district by elevation zone under each published scheme"
    )
    bands_cmd.add_argument("action", choices=("build", "zones"))
    bands_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    bands_cmd.set_defaults(run=_run_bands)

    observe_cmd = commands.add_parser(
        "observe", help="fetch-final, fetch-realtime: save IMD rainfall as write-once observations; "
        "base-rates: write the base-rate table from the saved files, or check it reproduces"
    )
    observe_cmd.add_argument("action", choices=("fetch-final", "fetch-realtime", "base-rates"))
    observe_cmd.add_argument("--years", type=int, nargs=2, metavar=("FIRST", "LAST"),
                             help="fetch-final: years to save")
    observe_cmd.add_argument("--dates", type=date.fromisoformat, nargs=2, metavar=("FIRST", "LAST"),
                             help="fetch-realtime: IMD dates YYYY-MM-DD to save as one record")
    observe_cmd.add_argument("--vintage", help="base-rates: the final vintage to use")
    observe_cmd.add_argument("--provisional", help="base-rates: a real-time vintage to set beside it")
    observe_cmd.add_argument("--check", action="store_true",
                             help="base-rates: exit 1 unless the committed table matches")
    observe_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    observe_cmd.set_defaults(run=_run_observe)

    issue_cmd = commands.add_parser(
        "issue", help="--date: append that issue date's claim per verdict band and view; --backfill-missing: "
        "record not_issued for started windows with no claim; --check: re-derive every claim"
    )
    action = issue_cmd.add_mutually_exclusive_group(required=True)
    action.add_argument("--date", type=date.fromisoformat, help="issue date YYYY-MM-DD")
    action.add_argument("--backfill-missing", action="store_true")
    action.add_argument("--check", action="store_true")
    issue_cmd.add_argument("--abstain-if-missing", action="store_true",
                           help="--date: with no saved run, record an abstain instead of failing")
    issue_cmd.add_argument("--year", type=int, help="--backfill-missing: season year (default: this year, UTC)")
    issue_cmd.add_argument("--bundle", default=DEV_BUNDLE,
                           help="bundle name (default: dev, whose entries are exploratory)")
    issue_cmd.add_argument("--ledger", default="mandi-wheat")
    issue_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    issue_cmd.set_defaults(run=_run_issue)

    score_cmd = commands.add_parser(
        "score", help="--vintage: append a score against that IMD vintage for every closed claim "
        "that can take one; --check: re-derive every score"
    )
    action = score_cmd.add_mutually_exclusive_group(required=True)
    action.add_argument("--vintage", help="observation vintage, like realtime-r20261027")
    action.add_argument("--check", action="store_true")
    score_cmd.add_argument("--kind", default="exploratory", choices=("exploratory", "live"),
                           help="--vintage: which claims file to score (default: exploratory)")
    score_cmd.add_argument("--ledger", default="mandi-wheat")
    score_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    score_cmd.set_defaults(run=_run_score)

    bundle_cmd = commands.add_parser(
        "bundle", help="power: write the power simulation table from the saved observations, "
        "or check it reproduces"
    )
    bundle_cmd.add_argument("action", choices=("power",))
    bundle_cmd.add_argument("--check", action="store_true", help="exit 1 unless the committed table matches")
    bundle_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    bundle_cmd.set_defaults(run=_run_bundle)

    args = parser.parse_args(argv)
    return args.run(args)


def _repo_root(args: argparse.Namespace) -> Path:
    return args.repo or Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())


def _run_verify(args: argparse.Namespace) -> int:
    try:
        repo = _repo_root(args)
        problems = verify(repo, args.base)
    except subprocess.CalledProcessError as e:
        command = " ".join(e.cmd)
        print(f"evidence verify: `{command}` failed: {e.stderr.decode().strip()}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"evidence verify: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("evidence verify: ok")
    return 0


def _run_archive(args: argparse.Namespace) -> int:
    from khetru_evidence import fetch_forecasts

    repo = _repo_root(args)
    day = args.date or datetime.now(UTC).date()
    hours = sorted(set(args.hours)) if args.hours else fetch_forecasts.RUN_HOURS
    return fetch_forecasts.archive(day, hours, repo=repo, source=args.source)


def _run_bands(args: argparse.Namespace) -> int:
    from khetru_evidence import fetch_bands

    run = {"build": fetch_bands.build, "zones": fetch_bands.zones}[args.action]
    return run(repo=_repo_root(args))


def _run_observe(args: argparse.Namespace) -> int:
    repo = _repo_root(args)
    if args.action == "base-rates":
        from khetru_evidence import observations

        if not args.vintage:
            print("evidence observe base-rates: --vintage is required", file=sys.stderr)
            return 2
        return observations.write_base_rates(
            repo=repo, vintage=args.vintage, provisional=args.provisional, check=args.check
        )
    from khetru_evidence import fetch_observations

    if args.action == "fetch-final":
        if not args.years:
            print("evidence observe fetch-final: --years is required", file=sys.stderr)
            return 2
        return fetch_observations.fetch_final(*args.years, repo=repo)
    if not args.dates:
        print("evidence observe fetch-realtime: --dates is required", file=sys.stderr)
        return 2
    return fetch_observations.fetch_realtime(*args.dates, repo=repo)


def _run_issue(args: argparse.Namespace) -> int:
    from khetru_evidence import claims

    repo = _repo_root(args)
    ledger = Ledger(repo / LEDGERS_DIR / args.ledger)
    kind = "exploratory" if args.bundle == DEV_BUNDLE else "live"
    try:
        if args.check:
            problems = [p for rel in claims.claim_files(ledger) for p in claims.check(ledger, rel)]
            for problem in problems:
                print(problem, file=sys.stderr)
            print(f"evidence issue: {len(problems)} problem(s)" if problems else "evidence issue: ok")
            return 1 if problems else 0
        # Live entries are written by the GitHub workflows only (KTD12).
        if kind == "live" and os.environ.get("GITHUB_ACTIONS") != "true":
            print("evidence issue: live entries are never written from a local machine", file=sys.stderr)
            return 2
        if args.backfill_missing:
            year = args.year or datetime.now(UTC).year
            written = claims.backfill_missing(ledger, year, bundle=args.bundle, kind=kind)
        else:
            code = _git(repo, "rev-parse", "HEAD").decode().strip()
            written = claims.issue(ledger, args.date, bundle=args.bundle, kind=kind, code=code,
                                   abstain_if_missing=args.abstain_if_missing)
    except ValueError as e:  # claim, ledger, record and band-map errors
        print(f"evidence issue: {e}", file=sys.stderr)
        return 1
    for e in written:
        print(f"evidence issue: {e['type']} {e['id']}" + (f": {e['statement']}" if "statement" in e else ""))
    if not written:
        print("evidence issue: nothing to write")
    return 0


def _run_score(args: argparse.Namespace) -> int:
    """Prints outcome labels and counts only: no Brier score or skill, whatever the bundle (KTD11)."""
    from khetru_evidence import claims, scoring

    repo = _repo_root(args)
    ledger = Ledger(repo / LEDGERS_DIR / args.ledger)
    try:
        if args.check:
            problems = [p for rel in claims.claim_files(ledger) for p in scoring.check(ledger, rel)]
            for problem in problems:
                print(problem, file=sys.stderr)
            print(f"evidence score: {len(problems)} problem(s)" if problems else "evidence score: ok")
            return 1 if problems else 0
        # Live entries are written by the GitHub workflows only (KTD12).
        if args.kind == "live" and os.environ.get("GITHUB_ACTIONS") != "true":
            print("evidence score: live entries are never written from a local machine", file=sys.stderr)
            return 2
        rel = claims.kind_file(args.kind)
        code = _git(repo, "rev-parse", "HEAD").decode().strip()
        written = scoring.score(ledger, rel, vintage=args.vintage, code=code)
    except ValueError as e:  # score, ledger, record and band-map errors
        print(f"evidence score: {e}", file=sys.stderr)
        return 1
    for e in written:
        print(f"evidence score: {e['id']}: {e['outcome']}" + (" (provisional)" if e["provisional"] else "")
              + (f"; {e['reason']}" if "reason" in e else ""))
    if not written:
        print("evidence score: nothing to write")
    scores = scoring.current(ledger.read(scoring.scores_file(rel)))
    counts = scoring.outcomes(scoring.registered(scores))
    print(f"evidence score: {args.kind} current scores: "
          + ", ".join(f"{counts[o]} {o}" for o in OUTCOMES))
    # A view is counted on its own line, never with the verdict bands.
    for band in sorted({e["band"] for e in scores} - {e["band"] for e in scoring.registered(scores)}):
        counts = scoring.outcomes(e for e in scores if e["band"] == band)
        print(f"evidence score: {args.kind} current scores, {band} (a view, never part of a verdict): "
              + ", ".join(f"{counts[o]} {o}" for o in OUTCOMES))
    return 0


def _run_bundle(args: argparse.Namespace) -> int:
    from khetru_evidence import bundle

    return bundle.write_power(repo=_repo_root(args), check=args.check)


def _run_same_record(args: argparse.Namespace) -> int:
    from khetru_evidence import forecasts

    first, second = (path.read_bytes() for path in args.records)
    return 0 if forecasts.same_run(first, second) else 1


def verify(repo: Path, base: str) -> list[str]:
    """Every integrity problem in ``repo``'s working tree relative to ``base``."""
    _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{tree}}")
    problems = _check_against_base(repo, base)
    ledgers_dir = repo / LEDGERS_DIR
    if ledgers_dir.is_dir():
        for root in sorted(p for p in ledgers_dir.iterdir() if p.is_dir()):
            problems += _check_ledger(repo, Ledger(root))
    return problems


def _check_against_base(repo: Path, base: str) -> list[str]:
    """JSONL files must extend their base bytes; write-once evidence and manifests must be unchanged."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--name-only", base, "--", LEDGERS_DIR)
    problems = []
    for name in filter(None, listing.decode().split("\0")):
        parts = PurePosixPath(name).parts
        if len(parts) < 3:
            continue
        write_once = parts[2] in (*WRITE_ONCE_DIRS, MANIFEST_DIR)
        if not write_once and not name.endswith(".jsonl"):
            continue
        current = repo / name
        if not current.exists():
            problems.append(f"{name}: missing; it exists at base {base}")
            continue
        old, new = _git(repo, "show", f"{base}:{name}"), current.read_bytes()
        if write_once:
            if new != old:
                problems.append(f"{name}: write-once evidence file changed since base {base}")
        elif not new.startswith(old):
            line = _first_changed_line(old, new)
            problems.append(f"{name}:{line}: line from base {base} was edited, deleted or reordered")
    return problems


def _first_changed_line(old: bytes, new: bytes) -> int:
    old_lines, new_lines = old.split(b"\n"), new.split(b"\n")
    for i, line in enumerate(old_lines):
        if i >= len(new_lines) or new_lines[i] != line:
            return i + 1
    return len(old_lines)


def _check_ledger(repo: Path, ledger: Ledger) -> list[str]:
    prefix = ledger.root.relative_to(repo).as_posix()
    problems = []
    for rel in ledger.ledger_files():
        for line, message in ledger.check_file(rel):
            where = f"{prefix}/{rel}:{line}" if line else f"{prefix}/{rel}"
            problems.append(f"{where}: {message}")
    for rel in ledger.manifests():
        for name, message in ledger.check_manifest(rel):
            problems.append(f"{prefix}/{rel}: {prefix}/{name} {message}")
    for entry in ledger.tags():
        name, recorded = entry.get("name"), entry.get("commit")
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{name}^{{commit}}"],
            cwd=repo, capture_output=True, text=True,
        )
        actual = result.stdout.strip()
        if result.returncode != 0:
            problems.append(f"{prefix}/tags.jsonl: tag {name} does not resolve (recorded at {recorded})")
        elif actual != recorded:
            problems.append(f"{prefix}/tags.jsonl: tag {name} resolves to {actual}, recorded at {recorded}")
    return problems


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


if __name__ == "__main__":
    sys.exit(main())

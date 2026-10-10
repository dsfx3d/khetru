"""`evidence` command line. Subcommands are added per unit: `verify` (U2), `bands` (U3), `archive` and `same-record` (U5)."""

import argparse
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from khetru_evidence.ledger import MANIFEST_DIR, WRITE_ONCE_DIRS, Ledger

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
        "bands", help="build the band map from its recorded sources; a rebuild must reproduce it"
    )
    bands_cmd.add_argument("action", choices=("build",))
    bands_cmd.add_argument("--repo", type=Path, help="repository root (default: current repo)")
    bands_cmd.set_defaults(run=_run_bands)

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

    return fetch_bands.build(repo=_repo_root(args))


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

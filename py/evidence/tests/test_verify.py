import json
import subprocess
from datetime import UTC, date, datetime

import pytest

from khetru_evidence import cli
from khetru_evidence import ledger as L

CLAIMS = "ledger/mandi-wheat/claims/exploratory.jsonl"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def fixed_clock():
    return datetime(2026, 10, 19, 10, 30, tzinfo=UTC)


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def claim(issue_date, **overrides):
    fields = dict(
        ledger="mandi-wheat", kind="exploratory", bundle="dev", band="district",
        issue_date=issue_date, probability=0.4,
    )
    fields.update(overrides)
    return L.claim(**fields)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")
    lg = L.Ledger(repo / "ledger" / "mandi-wheat", clock=fixed_clock)
    lg.append("claims/exploratory.jsonl", claim(date(2026, 10, 19)))
    lg.append("claims/exploratory.jsonl", claim(date(2026, 10, 26)))
    (lg.root / "inputs").mkdir()
    (lg.root / "inputs" / "ens-2026101900.json").write_text('{"members":[1.0,2.0]}\n')
    return repo


@pytest.fixture
def base(repo):
    return commit_all(repo, "base")


def ledger_of(repo):
    return L.Ledger(repo / "ledger" / "mandi-wheat", clock=fixed_clock)


def verify(repo, base, capsys):
    code = cli.main(["verify", "--base", base, "--repo", str(repo)])
    return code, capsys.readouterr()


def lines(repo, rel=CLAIMS):
    return (repo / rel).read_bytes().splitlines(keepends=True)


def write_lines(repo, items, rel=CLAIMS):
    (repo / rel).write_bytes(b"".join(items))


def test_passes_when_file_extends_base_with_valid_lines(repo, base, capsys):
    ledger_of(repo).append("claims/exploratory.jsonl", claim(date(2026, 11, 2)))

    code, out = verify(repo, base, capsys)

    assert code == 0, out.err
    assert "ok" in out.out


def test_passes_against_empty_tree_for_first_push(repo, base, capsys):
    code, out = verify(repo, EMPTY_TREE, capsys)
    assert code == 0, out.err


def test_fails_naming_file_and_line_when_old_line_edited(repo, base, capsys):
    old = lines(repo)
    write_lines(repo, [old[0].replace(b"0.4", b"0.5"), old[1]])

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{CLAIMS}:1:" in out.err


def test_fails_when_old_line_deleted(repo, base, capsys):
    old = lines(repo)
    write_lines(repo, [old[0]])

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{CLAIMS}:2:" in out.err


def test_fails_when_old_lines_reordered(repo, base, capsys):
    old = lines(repo)
    write_lines(repo, [old[1], old[0]])

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{CLAIMS}:1:" in out.err


def test_fails_when_ledger_file_deleted(repo, base, capsys):
    (repo / CLAIMS).unlink()
    code, out = verify(repo, base, capsys)
    assert code == 1
    assert CLAIMS in out.err and "missing" in out.err


def test_fails_on_non_canonical_new_line(repo, base, capsys):
    entry = {**claim(date(2026, 11, 2)), "recorded_at": "2026-10-19T10:30:00Z"}
    write_lines(repo, lines(repo) + [json.dumps(entry).encode() + b"\n"])

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{CLAIMS}:3:" in out.err and "canonical" in out.err


def test_fails_on_schema_invalid_new_line(repo, base, capsys):
    entry = {**claim(date(2026, 11, 2)), "recorded_at": "2026-10-19T10:30:00Z"}
    del entry["band"]
    write_lines(repo, lines(repo) + [L.encode(entry)])

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{CLAIMS}:3:" in out.err and "band" in out.err


def test_fails_on_id_that_does_not_match_natural_key(repo, base, capsys):
    entry = {**claim(date(2026, 11, 2)), "recorded_at": "2026-10-19T10:30:00Z"}
    entry["id"] = "made-up"
    write_lines(repo, lines(repo) + [L.encode(entry)])

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{CLAIMS}:3:" in out.err and "natural key" in out.err


def test_fails_on_duplicate_natural_key_added_by_hand(repo, base, capsys):
    old = lines(repo)
    write_lines(repo, old + [old[0]])

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{CLAIMS}:3:" in out.err and "duplicate" in out.err


def test_fails_on_hindcast_entry_in_live_file(repo, base, capsys):
    entry = {
        **claim(date(2026, 10, 19), kind="hindcast", bundle="v1", run_id="v1-run-1"),
        "recorded_at": "2026-10-19T10:30:00Z",
    }
    live = "ledger/mandi-wheat/claims/live.jsonl"
    write_lines(repo, [L.encode(entry)], rel=live)

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{live}:1:" in out.err and "only holds live" in out.err


def test_fails_on_changed_write_once_input(repo, base, capsys):
    rel = "ledger/mandi-wheat/inputs/ens-2026101900.json"
    (repo / rel).write_text('{"members":[1.0,9.0]}\n')

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert rel in out.err and "write-once" in out.err


def test_new_write_once_files_may_be_added(repo, base, capsys):
    (repo / "ledger/mandi-wheat/inputs/ens-2026102600.json").write_text("{}\n")
    code, out = verify(repo, base, capsys)
    assert code == 0, out.err


def test_fails_on_file_shorter_than_stamped_manifest(repo, base, capsys):
    lg = ledger_of(repo)
    lg.append("claims/exploratory.jsonl", claim(date(2026, 11, 2)))
    manifest = lg.write_manifest()
    code, out = verify(repo, base, capsys)
    assert code == 0, out.err

    write_lines(repo, lines(repo)[:2])
    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"ledger/mandi-wheat/{manifest}" in out.err
    assert CLAIMS in out.err and "shorter" in out.err


def test_fails_on_tag_that_now_resolves_to_different_commit(repo, base, capsys):
    git(repo, "tag", "-a", "mandi-wheat/bundle-v1", "-m", "bundle v1", base)
    ledger_of(repo).append("tags.jsonl", L.tag(ledger="mandi-wheat", name="mandi-wheat/bundle-v1", commit=base))
    head = commit_all(repo, "record tag")
    code, out = verify(repo, base, capsys)
    assert code == 0, out.err

    git(repo, "tag", "-f", "-a", "mandi-wheat/bundle-v1", "-m", "moved", head)
    code, out = verify(repo, base, capsys)

    assert code == 1
    assert "mandi-wheat/bundle-v1" in out.err and base in out.err


def test_fails_on_recorded_tag_that_was_deleted(repo, base, capsys):
    git(repo, "tag", "-a", "mandi-wheat/bundle-v1", "-m", "bundle v1", base)
    ledger_of(repo).append("tags.jsonl", L.tag(ledger="mandi-wheat", name="mandi-wheat/bundle-v1", commit=base))
    git(repo, "tag", "-d", "mandi-wheat/bundle-v1")

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert "mandi-wheat/bundle-v1" in out.err and "does not resolve" in out.err


def test_fails_on_stamped_prefix_edited_then_extended(repo, base, capsys):
    lg = ledger_of(repo)
    lg.append("claims/exploratory.jsonl", claim(date(2026, 11, 2)))
    manifest = lg.write_manifest()
    old = lines(repo)
    write_lines(repo, [*old[:2], old[2].replace(b"0.4", b"0.5")])  # same length, not in base
    lg.append("claims/exploratory.jsonl", claim(date(2026, 11, 9)))

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"ledger/mandi-wheat/{manifest}: {CLAIMS} does not extend" in out.err


def test_fails_when_file_in_stamped_manifest_is_missing(repo, base, capsys):
    lg = ledger_of(repo)
    lg.append("claims/live.jsonl", claim(date(2026, 11, 2), kind="live", bundle="v1"))
    manifest = lg.write_manifest()
    (lg.root / "claims/live.jsonl").unlink()

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"ledger/mandi-wheat/{manifest}: ledger/mandi-wheat/claims/live.jsonl is missing" in out.err


def test_fails_on_hindcast_claim_filed_under_another_run(repo, base, capsys):
    lg = ledger_of(repo)
    for seq in (1, 2):
        lg.append("hindcast/v1/runs.jsonl", L.run(ledger="mandi-wheat", bundle="v1", seq=seq, status="started"))
    entry = {
        **claim(date(2026, 10, 19), kind="hindcast", bundle="v1", run_id="v1-run-2"),
        "recorded_at": "2026-10-19T10:30:00Z",
    }
    misfiled = "ledger/mandi-wheat/hindcast/v1/v1-run-1/claims.jsonl"
    (repo / misfiled).parent.mkdir(parents=True)
    write_lines(repo, [L.encode(entry)], rel=misfiled)

    code, out = verify(repo, base, capsys)

    assert code == 1
    assert f"{misfiled}:1:" in out.err and "v1-run-2" in out.err


@pytest.fixture
def stamped(repo, base):
    ledger_of(repo).write_manifest()
    return commit_all(repo, "stamp")


def test_fails_on_edited_committed_manifest(repo, stamped, capsys):
    path = next((repo / "ledger/mandi-wheat/manifests").iterdir())
    manifest = json.loads(path.read_bytes())
    manifest["files"] = {}
    path.write_bytes(L.encode(manifest))

    code, out = verify(repo, stamped, capsys)

    assert code == 1
    assert "manifests/" in out.err and "write-once" in out.err


def test_fails_on_deleted_committed_manifest(repo, stamped, capsys):
    path = next((repo / "ledger/mandi-wheat/manifests").iterdir())
    path.unlink()

    code, out = verify(repo, stamped, capsys)

    assert code == 1
    assert "manifests/" in out.err and "missing" in out.err

import hashlib
import json
from datetime import UTC, date, datetime

import pytest

from khetru_evidence import ledger as L

CLAIMS = "claims/exploratory.jsonl"
SCORES = "scores/exploratory.jsonl"


def fixed_clock():
    return datetime(2026, 10, 19, 10, 30, tzinfo=UTC)


@pytest.fixture
def lg(tmp_path):
    return L.Ledger(tmp_path / "ledger" / "mandi-wheat", clock=fixed_clock)


def claim(**overrides):
    fields = dict(
        ledger="mandi-wheat",
        kind="exploratory",
        bundle="dev",
        band="district",
        issue_date=date(2026, 10, 19),
        probability=0.4,
    )
    fields.update(overrides)
    return L.claim(**fields)


def test_append_writes_one_canonical_line_and_reads_back_equal(lg):
    written = lg.append(CLAIMS, claim())

    raw = (lg.root / CLAIMS).read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    expected = json.dumps(written, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert raw == expected.encode("utf-8") + b"\n"
    assert b'"recorded_at":"2026-10-19T10:30:00Z"' in raw
    assert lg.read(CLAIMS) == [written]


def test_ids_derive_from_natural_key_not_time():
    a = claim()
    b = claim(probability=0.9)
    assert a["id"] == b["id"] == "mandi-wheat:exploratory:dev:district:2026-10-19"
    assert claim(band="zone-ii")["id"] != a["id"]


def test_append_refuses_file_without_trailing_newline(lg):
    lg.append(CLAIMS, claim())
    path = lg.root / CLAIMS
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    before = path.read_bytes()

    with pytest.raises(L.LedgerError, match="trailing newline"):
        lg.append(CLAIMS, claim(issue_date=date(2026, 10, 26)))
    assert path.read_bytes() == before


def test_append_refuses_second_claim_for_same_key(lg):
    lg.append(CLAIMS, claim())
    before = (lg.root / CLAIMS).read_bytes()

    with pytest.raises(L.LedgerError, match="duplicate"):
        lg.append(CLAIMS, claim(probability=0.7))
    with pytest.raises(L.LedgerError, match="duplicate"):
        lg.append(CLAIMS, L.abstain(**_key(), reason="source unavailable"))
    assert (lg.root / CLAIMS).read_bytes() == before


def test_append_refuses_not_issued_once_claim_exists(lg):
    lg.append(CLAIMS, claim())
    before = (lg.root / CLAIMS).read_bytes()

    with pytest.raises(L.LedgerError, match="not_issued refused"):
        lg.append(CLAIMS, L.not_issued(**_key()))
    assert (lg.root / CLAIMS).read_bytes() == before


def test_key_order_does_not_change_bytes():
    a = {"kind": "exploratory", "b": {"y": 1, "x": "ठंड"}, "a": [1, 2]}
    b = {"a": [1, 2], "b": {"x": "ठंड", "y": 1}, "kind": "exploratory"}
    assert L.encode(a) == L.encode(b)
    assert L.encode(a) == '{"a":[1,2],"b":{"x":"ठंड","y":1},"kind":"exploratory"}\n'.encode()


def test_dev_bundle_entries_are_always_exploratory(lg):
    with pytest.raises(L.LedgerError, match="dev bundle"):
        lg.append("claims/live.jsonl", claim(kind="live"))
    assert not (lg.root / "claims/live.jsonl").exists()


def test_file_placement_by_kind(lg):
    with pytest.raises(L.LedgerError, match="only holds live"):
        lg.append("claims/live.jsonl", claim())
    with pytest.raises(L.LedgerError, match="only holds exploratory"):
        lg.append(CLAIMS, claim(kind="live", bundle="v1"))


def test_hindcast_entries_need_a_started_run(lg):
    run_file = "hindcast/v1/runs.jsonl"
    hc_claims = "hindcast/v1/v1-run-1/claims.jsonl"
    hc = claim(kind="hindcast", bundle="v1", run_id="v1-run-1")

    with pytest.raises(L.LedgerError, match="no started entry"):
        lg.append(hc_claims, hc)

    started = lg.append(run_file, L.run(ledger="mandi-wheat", bundle="v1", seq=1, status="started"))
    assert started["run_id"] == "v1-run-1"
    lg.append(hc_claims, hc)
    assert lg.read(hc_claims)[0]["id"] == "mandi-wheat:hindcast:v1:v1-run-1:district:2026-10-19"


def test_score_supersede_chain(lg):
    c = lg.append(CLAIMS, claim())
    first = lg.append(SCORES, _score(c, "imd-rt-2026-10-28"))

    with pytest.raises(L.LedgerError, match="current score"):
        lg.append(SCORES, _score(c, "imd-final-2027"))
    with pytest.raises(L.LedgerError, match="reason"):
        lg.append(SCORES, _score(c, "imd-final-2027", supersedes=first["id"]))

    second = lg.append(
        SCORES, _score(c, "imd-final-2027", supersedes=first["id"], reason="final IMD data")
    )
    assert second["id"] == f"{c['id']}@imd-final-2027"

    # A fork off the old head is refused.
    with pytest.raises(L.LedgerError, match="current score"):
        lg.append(SCORES, _score(c, "imd-final-2028", supersedes=first["id"], reason="x"))
    with pytest.raises(L.LedgerError, match="duplicate"):
        lg.append(SCORES, _score(c, "imd-rt-2026-10-28", supersedes=second["id"], reason="x"))


def test_corrections_void_or_fix_non_scoring_fields(lg):
    c = lg.append(CLAIMS, claim())

    with pytest.raises(L.LedgerError, match="affects scoring"):
        lg.append(CLAIMS, _correction(c, 1, action="fix", field="probability", value=0.9))
    with pytest.raises(L.LedgerError, match="unknown entry"):
        lg.append(CLAIMS, _correction({**c, "id": c["id"] + "x"}, 1, action="void"))

    lg.append(CLAIMS, _correction(c, 1, action="fix", field="note", value="typo in note"))
    voided = lg.append(CLAIMS, _correction(c, 2, action="void"))
    assert voided["id"] == f"{c['id']}#2"
    with pytest.raises(L.LedgerError, match="voided"):
        lg.append(CLAIMS, _correction(c, 3, action="void"))
    with pytest.raises(L.LedgerError, match="sequence"):
        lg.append(CLAIMS, _correction(c, 5, action="fix", field="note", value="y"))


def test_manifest_records_length_and_sha256_of_every_ledger_file(lg):
    lg.append(CLAIMS, claim())
    lg.append(SCORES, _score(lg.read(CLAIMS)[0], "imd-rt-2026-10-28"))

    rel = lg.write_manifest()

    assert rel == "manifests/2026-10-19T103000Z.json"
    raw = (lg.root / rel).read_bytes()
    manifest = json.loads(raw)
    assert raw == L.encode(manifest)
    assert manifest["created_at"] == "2026-10-19T10:30:00Z"
    for name in (CLAIMS, SCORES):
        data = (lg.root / name).read_bytes()
        assert manifest["files"][name] == {
            "length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    with pytest.raises(L.LedgerError, match="exists"):
        lg.write_manifest()


TS = "2026-10-19T10:30:00Z"
SHA = "0" * 40


@pytest.mark.parametrize("rel, entry, message", [
    (CLAIMS, claim(ledger="other"), "belongs to ledger 'other'"),
    (SCORES, L.score(ledger="mandi-wheat", claim_id="c", kind="exploratory", bundle="dev",
                     vintage="v", outcome="maybe"), "outcome must be one of"),
    ("tags.jsonl", L.tag(ledger="mandi-wheat", name="t", commit="abc"), "full 40-hex SHA"),
    (CLAIMS, {**claim(), "recorded_at": "2026-10-19"}, "recorded_at must be UTC"),
    (CLAIMS, claim(issue_date="2026-13-40"), "issue_date must be an ISO date"),
    ("hindcast/v1/runs.jsonl", L.run(ledger="mandi-wheat", bundle="v1", seq=0, status="started"),
     "seq must be a positive integer"),
    ("hindcast/v1/runs.jsonl", L.run(ledger="mandi-wheat", bundle="v1", seq=True, status="started"),
     "seq must be a positive integer"),
    ("hindcast/v1/runs.jsonl", L.run(ledger="mandi-wheat", bundle="v1", seq=1, status="paused"),
     "run status must be one of"),
    ("hindcast/v2/runs.jsonl", L.run(ledger="mandi-wheat", bundle="v1", seq=1, status="started"),
     "only holds bundle v2 entries"),
    ("claims/foo.jsonl", claim(), "not a recognised ledger file"),
], ids=["ledger", "outcome", "tag-commit", "recorded-at", "issue-date", "seq-0", "seq-bool",
        "run-status", "run-bundle", "stray-file"])
def test_check_file_rejects(lg, rel, entry, message):
    path = lg.root / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(L.encode({"recorded_at": TS, **entry}))

    problems = lg.check_file(rel)

    assert any(message in m for _, m in problems), problems


def _key():
    return dict(
        ledger="mandi-wheat", kind="exploratory", bundle="dev", band="district",
        issue_date=date(2026, 10, 19),
    )


def _score(c, vintage, **extra):
    return L.score(
        claim_id=c["id"], ledger="mandi-wheat", kind=c["kind"], bundle=c["bundle"], vintage=vintage,
        outcome="held", **extra,
    )


def _correction(c, seq, **extra):
    return L.correction(
        target_id=c["id"], ledger="mandi-wheat", kind=c["kind"], bundle=c["bundle"],
        seq=seq, reason="test", **extra,
    )

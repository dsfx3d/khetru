"""U6: the claim core and the live/exploratory driver. Forecast records are synthetic."""

import dataclasses
from datetime import UTC, date, datetime

import pytest

from claim_fixtures import BAND_MAP, CODE, DEV_BUNDLE, MONDAY, REAL_LEDGER, TOTALS_30_OF_51, make_ledger, record
from khetru_evidence import bands as B
from khetru_evidence import claim_core as C
from khetru_evidence import claims, cli, forecasts as fc, obs_core, semantics
from khetru_evidence import ledger as L

EXPLORATORY = "claims/exploratory.jsonl"
AFTER_RUN = datetime(2026, 10, 19, 10, 30, tzinfo=UTC)
EVIDENCE = {"bands/band-map.csv": "cd" * 32}


@pytest.fixture
def sem():
    return semantics.load(DEV_BUNDLE / "bundle.toml")


@pytest.fixture
def rule():
    return C.load_rule(DEV_BUNDLE / "claims.toml")


def make(sem, rule, rec, band_map=BAND_MAP, coverage_share=1.0):
    return C.make_claim(sem=sem, rule=rule, coverage_share=coverage_share, band_map=band_map,
                        verdict_band=B.DISTRICT, issue_date=MONDAY, record=rec, evidence=EVIDENCE)


# --- the core ----------------------------------------------------------------


def test_30_of_51_members_at_or_above_10_mm_give_the_bundle_formulas_probability(sem, rule):
    body = make(sem, rule, record(TOTALS_30_OF_51))
    assert body["type"] == "claim"
    assert (body["members_at_or_above"], body["members"]) == (30, 51)
    assert rule.probability_formula == "(k + 0.5) / (n + 1)"
    assert body["probability"] == round(30.5 / 52, 6)
    assert body["statement"] == "≥10 mm rain in band district within 7 days: 59%"


def test_the_probability_is_never_0_or_1():
    for formula in C.FORMULAS:
        assert 0 < C.probability(formula, 0, 51) < C.probability(formula, 51, 51) < 1
    with pytest.raises(C.ClaimError):
        C.probability("k / n", 30, 51)


def test_no_record_for_the_date_is_an_abstain_that_says_why(sem, rule):
    body = make(sem, rule, None)
    assert body["type"] == "abstain"
    assert body["statement"] == "can't tell — no forecast coverage for this band"
    assert body["reason"] == "no forecast coverage for this band"
    assert "probability" not in body


def test_a_forecast_short_of_the_coverage_share_is_an_abstain(sem, rule):
    one_cell = record(TOTALS_30_OF_51, lons=(77.0,))
    body = make(sem, rule, one_cell)
    assert (body["type"], body["coverage"]) == ("abstain", 0.75)
    assert body["statement"] == "can't tell — no forecast coverage for this band"
    assert make(sem, rule, one_cell, coverage_share=0.75)["type"] == "claim"


def test_every_r1_field_is_present_and_the_expiry_is_the_window_end(sem, rule):
    body = make(sem, rule, record(TOTALS_30_OF_51))
    times = semantics.schedule(sem, MONDAY)
    assert body["band"] == "district"
    assert body["crop_stage"] == "wheat sowing"
    assert (body["window_start"], body["window_end"]) == ("2026-10-20T03:00:00Z", "2026-10-27T03:00:00Z")
    assert body["expiry"] == L.timestamp(times.window_end)
    assert body["issue_date"] == "2026-10-19"
    assert body["source"] == "ECMWF ENS open data"
    assert (body["run_init"], body["step_start_hours"], body["step_end_hours"]) == ("2026-10-19T00:00:00Z", 24, 192)
    assert body["evidence"] == EVIDENCE
    assert {"statement", "probability", "rule", "threshold_mm"} <= set(body)


def test_a_record_of_another_run_is_refused(sem, rule):
    with pytest.raises(C.ClaimError):
        make(sem, rule, record(TOTALS_30_OF_51, init=datetime(2026, 10, 19, 12, tzinfo=UTC)))


def test_a_saved_rabi_2026_run_gives_a_claim_on_the_real_band_map(sem, rule):
    """Pipeline check only (KTD11): a real saved run goes through the core end to end."""
    saved = sorted((REAL_LEDGER / "inputs").glob("ens-opendata-2026*00.json.gz"))[0]
    rec = fc.decode_record(saved.read_bytes())
    day = fc.record_init(rec).date()
    any_day = dataclasses.replace(sem, issue_weekday=day.weekday(), season_start=(day.month, day.day))
    band_map = B.decode((REAL_LEDGER / B.BAND_MAP_PATH).read_bytes())
    share = obs_core.load_rules(DEV_BUNDLE / "observations.toml").coverage_share
    body = C.make_claim(sem=any_day, rule=rule, coverage_share=share, band_map=band_map,
                        verdict_band=B.DISTRICT, issue_date=day, record=rec, evidence={})
    assert (body["type"], body["members"], body["coverage"]) == ("claim", 51, 1.0)
    assert 0 < body["probability"] < 1


# --- the driver --------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", AFTER_RUN)
    fc.write_record(lg.root, record(TOTALS_30_OF_51))
    return lg


def issue(lg, day=MONDAY, **options):
    return claims.issue(lg, day, **{"bundle": "dev", "kind": "exploratory", "code": CODE, **options})


def test_issue_writes_one_exploratory_claim_naming_its_evidence_files(ledger):
    [entry] = issue(ledger)
    assert ledger.read(EXPLORATORY) == [entry]
    assert entry["id"] == "mandi-wheat:exploratory:dev:district:2026-10-19"
    assert (entry["type"], entry["kind"], entry["code"]) == ("claim", "exploratory", CODE)
    assert sorted(entry["evidence"]) == [
        "bands/band-map.csv", "bundles/dev/bundle.toml", "bundles/dev/claims.toml",
        "bundles/dev/observations.toml", "inputs/ens-opendata-2026101900.json.gz",
    ]


def test_issuing_the_same_date_twice_writes_nothing_the_second_time(ledger):
    issue(ledger)
    before = (ledger.root / EXPLORATORY).read_bytes()
    assert issue(ledger) == []
    assert (ledger.root / EXPLORATORY).read_bytes() == before


def test_issue_refuses_before_the_run_can_be_available(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", datetime(2026, 10, 19, 8, 59, tzinfo=UTC))
    fc.write_record(lg.root, record(TOTALS_30_OF_51))
    with pytest.raises(C.ClaimError, match="not available"):
        issue(lg)
    assert not (lg.root / EXPLORATORY).exists()


def test_with_no_saved_run_issue_refuses_unless_told_to_abstain(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", AFTER_RUN)
    with pytest.raises(C.ClaimError, match="no saved opendata record"):
        issue(lg)
    assert not (lg.root / EXPLORATORY).exists()
    [entry] = issue(lg, abstain_if_missing=True)
    assert (entry["type"], entry["reason"]) == ("abstain", "no forecast coverage for this band")


def test_an_exploratory_claim_may_be_issued_after_its_window_but_a_live_one_may_not(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", datetime(2026, 11, 20, tzinfo=UTC))
    fc.write_record(lg.root, record(TOTALS_30_OF_51))
    with pytest.raises(C.ClaimError, match="never back-filled"):
        issue(lg, kind="live")
    assert [e["type"] for e in issue(lg)] == ["claim"]


def test_backfill_writes_not_issued_for_started_windows_only_and_never_a_claim(tmp_path):
    # 27 Oct 12:00: the 19 and 26 Oct windows have started; 2 and 9 Nov have not.
    lg = make_ledger(tmp_path / "mandi-wheat", datetime(2026, 10, 27, 12, tzinfo=UTC))
    fc.write_record(lg.root, record(TOTALS_30_OF_51))
    issue(lg)
    written = claims.backfill_missing(lg, 2026, bundle="dev", kind="exploratory")
    assert [(e["type"], e["issue_date"]) for e in written] == [("not_issued", "2026-10-26")]
    assert [e["type"] for e in lg.read(EXPLORATORY)] == ["claim", "not_issued"]
    assert claims.backfill_missing(lg, 2026, bundle="dev", kind="exploratory") == []
    # The slot is spent: a claim for it is no longer written.
    fc.write_record(lg.root, record(TOTALS_30_OF_51, init=datetime(2026, 10, 26, tzinfo=UTC)))
    assert issue(lg, date(2026, 10, 26)) == []


def test_every_claim_re_derives_from_the_files_it_names(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", datetime(2026, 10, 27, 12, tzinfo=UTC))
    fc.write_record(lg.root, record(TOTALS_30_OF_51))
    issue(lg)
    issue(lg, date(2026, 10, 26), abstain_if_missing=True)
    assert claims.check(lg, EXPLORATORY) == []
    # A run archived after the abstain does not change what the abstain was made from.
    fc.write_record(lg.root, record(TOTALS_30_OF_51, init=datetime(2026, 10, 26, tzinfo=UTC)))
    assert claims.check(lg, EXPLORATORY) == []


def test_check_reports_a_claim_whose_evidence_file_changed(ledger):
    [entry] = issue(ledger)
    (ledger.root / B.BAND_MAP_PATH).write_bytes(B.encode(B.BandMap(
        cells=(B.Cell(31.5, 77.0, 400.0, 1.0, B.DISTRICT),), reason=B.NO_INDEPENDENT_GAUGES)))
    assert claims.check(ledger, EXPLORATORY) == [
        f"{entry['id']}: evidence file bands/band-map.csv is missing or changed"]


def test_the_issue_command_never_writes_live_entries_from_a_local_machine(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    make_ledger(tmp_path / "ledger/mandi-wheat", AFTER_RUN)
    status = cli.main(["issue", "--date", "2026-10-19", "--bundle", "v1", "--repo", str(tmp_path)])
    assert status == 2
    assert "never written from a local machine" in capsys.readouterr().err
    assert not (tmp_path / "ledger/mandi-wheat/claims").exists()

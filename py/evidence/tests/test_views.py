"""Exploratory view bands: issued and scored beside the registered bands, never part of a verdict.

Inputs are synthetic but for the checks of the committed view file. The view is
the fixtures' cell at 77.25 E alone, which holds a quarter of the district band.
"""

import hashlib
import shutil
from datetime import UTC, date, datetime

import pytest

from claim_fixtures import BAND_MAP, CODE, MONDAY, REAL_LEDGER, TOTALS_30_OF_51, make_ledger, record
from khetru_evidence import bands as B
from khetru_evidence import claims, cli, forecasts as fc, obs_core, observations, scoring, semantics, views
from khetru_evidence import ledger as L
from score_fixtures import AFTER_WINDOW, FINAL, REALTIME, WINDOW_FIRST, save_rain, scoring_ledger, week

VIEW = "view-east-cell"
VIEW_PATH = f"bands/{VIEW}.csv"
VIEW_MAP = B.BandMap(cells=(B.Cell(31.5, 77.25, 100.0, 1.0, VIEW),), reason="")
EXPLORATORY, LIVE = "claims/exploratory.jsonl", "claims/live.jsonl"
AFTER_RUN = datetime(2026, 10, 19, 10, 30, tzinfo=UTC)
FOOTHILL = "view-foothill-cell"


def add_view(lg: L.Ledger, band_map: B.BandMap = VIEW_MAP, name: str = VIEW) -> L.Ledger:
    (lg.root / f"bands/{name}.csv").write_bytes(B.encode(band_map))
    return lg


def uneven_record(day: date = MONDAY) -> dict:
    """``TOTALS_30_OF_51`` at 77.00 E and half of it at 77.25 E: 29 members reach 10 mm in the district, none in the view."""
    rec = record(TOTALS_30_OF_51, init=datetime(day.year, day.month, day.day, tzinfo=UTC))
    for series in (rec["control"], *rec["perturbed"].values()):
        series[2][0][1] = round(1.0 + (series[2][0][0] - 1.0) / 2, 3)
    return rec


def issue(lg, day=MONDAY, **options):
    return claims.issue(lg, day, **{"bundle": "dev", "kind": "exploratory", "code": CODE, **options})


@pytest.fixture
def ledger(tmp_path):
    lg = add_view(make_ledger(tmp_path / "mandi-wheat", AFTER_RUN))
    fc.write_record(lg.root, uneven_record())
    return lg


def with_v1(lg: L.Ledger) -> L.Ledger:
    shutil.copytree(lg.root / "bundles/dev", lg.root / "bundles/v1")
    return lg


# --- the view files ----------------------------------------------------------


def test_the_areas_are_the_registered_map_then_each_view_file(ledger):
    registered, view = views.areas(ledger.root)
    assert (registered.path.name, registered.band_map) == ("band-map.csv", BAND_MAP)
    assert (view.path.name, view.band_map) == (f"{VIEW}.csv", VIEW_MAP)
    assert views.is_view(VIEW) and not views.is_view(B.DISTRICT)
    assert sorted(views.by_band(ledger.root)) == [B.DISTRICT, VIEW]


@pytest.mark.parametrize("band_map, name, message", [
    (VIEW_MAP, "view-other-name", "must hold the one band view-other-name"),
    (B.BandMap(cells=(B.Cell(31.5, 77.0, 300.0, 0.75, VIEW), B.Cell(31.5, 77.25, 100.0, 0.25, "view-b")),
               reason=""), VIEW, "must hold the one band"),
])
def test_a_view_file_that_does_not_hold_exactly_the_band_it_is_named_after_is_refused(
        tmp_path, band_map, name, message):
    lg = add_view(make_ledger(tmp_path / "mandi-wheat", AFTER_RUN), band_map, name)
    with pytest.raises(B.BandMapError, match=message):
        views.areas(lg.root)


def test_a_registered_band_may_not_carry_the_view_prefix(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", AFTER_RUN)
    (lg.root / B.BAND_MAP_PATH).write_bytes(B.encode(VIEW_MAP))
    with pytest.raises(B.BandMapError, match="registered band"):
        views.areas(lg.root)


# --- issuing -----------------------------------------------------------------


def test_issue_writes_one_exploratory_claim_per_registered_band_and_one_for_the_view(ledger):
    district, view = issue(ledger)
    assert ledger.read(EXPLORATORY) == [district, view]
    assert [e["id"] for e in (district, view)] == [
        "mandi-wheat:exploratory:dev:district:2026-10-19", f"mandi-wheat:exploratory:dev:{VIEW}:2026-10-19"]
    assert (district["kind"], view["kind"]) == ("exploratory", "exploratory")
    # Same rule, same record, different weights.
    assert (district["members_at_or_above"], view["members_at_or_above"]) == (29, 0)
    assert view["probability"] == round(0.5 / 52, 6)
    assert view["statement"] == f"≥10 mm rain in band {VIEW} within 7 days: 1%"
    assert {k: view[k] for k in ("rule", "threshold_mm", "window_start", "window_end", "run_init")} == {
        k: district[k] for k in ("rule", "threshold_mm", "window_start", "window_end", "run_init")}
    assert issue(ledger) == []


def test_each_claim_names_its_own_area_file_and_not_the_others(ledger):
    district, view = issue(ledger)
    shared = ["bundles/dev/bundle.toml", "bundles/dev/claims.toml", "bundles/dev/observations.toml",
              "inputs/ens-opendata-2026101900.json.gz"]
    assert sorted(district["evidence"]) == sorted(["bands/band-map.csv", *shared])
    assert sorted(view["evidence"]) == sorted([VIEW_PATH, *shared])


def test_the_view_is_exploratory_even_under_a_bundle_that_issues_live(ledger):
    with_v1(ledger)
    district, view = issue(ledger, kind="live", bundle="v1")
    assert (district["kind"], view["kind"]) == ("live", "exploratory")
    assert ledger.read(LIVE) == [district]
    assert ledger.read(EXPLORATORY) == [view]
    assert view["id"] == f"mandi-wheat:exploratory:v1:{VIEW}:2026-10-19"
    assert issue(ledger, kind="live", bundle="v1") == []


def test_the_registered_map_and_a_district_claim_are_unchanged_by_the_views_presence(tmp_path):
    plain = make_ledger(tmp_path / "plain/mandi-wheat", AFTER_RUN)
    fc.write_record(plain.root, uneven_record())
    [alone] = issue(plain)
    viewed = make_ledger(tmp_path / "viewed/mandi-wheat", AFTER_RUN)
    before = (viewed.root / B.BAND_MAP_PATH).read_bytes()
    add_view(viewed)
    fc.write_record(viewed.root, uneven_record())
    beside, _ = issue(viewed)
    assert beside == alone
    assert (viewed.root / B.BAND_MAP_PATH).read_bytes() == before == B.encode(BAND_MAP)


def test_a_view_added_after_the_district_claim_gets_its_own_claim_and_no_second_district_one(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", AFTER_RUN)
    fc.write_record(lg.root, uneven_record())
    [district] = issue(lg)
    add_view(lg)
    [view] = issue(lg)
    assert view["band"] == VIEW
    assert lg.read(EXPLORATORY) == [district, view]
    assert claims.check(lg, EXPLORATORY) == []


def test_the_coverage_rule_applies_to_the_view_with_its_own_weights(tmp_path):
    lg = add_view(make_ledger(tmp_path / "mandi-wheat", AFTER_RUN))
    fc.write_record(lg.root, record(TOTALS_30_OF_51, lons=(77.25,)))
    district, view = issue(lg)
    assert (district["type"], district["coverage"]) == ("abstain", 0.25)
    assert (view["type"], view["coverage"]) == ("claim", 1.0)

    other = add_view(make_ledger(tmp_path / "other/mandi-wheat", AFTER_RUN))
    fc.write_record(other.root, record(TOTALS_30_OF_51, lons=(77.0,)))
    district, view = issue(other)
    assert (district["type"], district["coverage"]) == ("abstain", 0.75)
    assert (view["type"], view["coverage"]) == ("abstain", 0.0)


def test_backfill_records_the_views_missing_slots_as_exploratory(tmp_path):
    lg = with_v1(add_view(make_ledger(tmp_path / "mandi-wheat", datetime(2026, 10, 27, 12, tzinfo=UTC))))
    written = claims.backfill_missing(lg, 2026, bundle="v1", kind="live")
    assert [(e["band"], e["kind"], e["issue_date"]) for e in written] == [
        (B.DISTRICT, "live", "2026-10-19"), (VIEW, "exploratory", "2026-10-19"),
        (B.DISTRICT, "live", "2026-10-26"), (VIEW, "exploratory", "2026-10-26")]
    assert {e["kind"] for e in lg.read(EXPLORATORY)} == {"exploratory"}
    assert claims.backfill_missing(lg, 2026, bundle="v1", kind="live") == []


def test_check_re_derives_the_views_claims_and_reports_a_changed_view_file(ledger):
    with_v1(ledger)
    _, view = issue(ledger)
    issue(ledger, kind="live", bundle="v1")
    assert claims.check(ledger, EXPLORATORY) == claims.check(ledger, LIVE) == []

    forged = [{**e, "probability": 0.9} if e["id"] == view["id"] else e for e in ledger.read(EXPLORATORY)]
    (ledger.root / EXPLORATORY).write_bytes(b"".join(L.encode(e) for e in forged))
    assert claims.check(ledger, EXPLORATORY) == [f"{view['id']}: does not re-derive from its evidence files"]

    (ledger.root / EXPLORATORY).write_bytes(L.encode(view))
    add_view(ledger, B.BandMap(cells=(B.Cell(31.5, 77.0, 300.0, 1.0, VIEW),), reason=""))
    assert claims.check(ledger, EXPLORATORY) == [f"{view['id']}: evidence file {VIEW_PATH} is missing or changed"]
    # The district's claim names the registered map only, so it still re-derives.
    assert claims.check(ledger, LIVE) == []


# --- the ledger refuses a view band anywhere but exploratory -------------------


def test_the_ledger_refuses_a_live_or_hindcast_entry_for_a_view_band(ledger):
    key = dict(ledger=ledger.name, bundle="v1", band=VIEW, issue_date=MONDAY)
    for make in (L.claim, L.not_issued):
        with pytest.raises(L.LedgerError, match="view band entries are always exploratory"):
            ledger.append(LIVE, make(kind="live", **key))
    ledger.append("hindcast/v1/runs.jsonl", L.run(ledger=ledger.name, bundle="v1", seq=1, status="started"))
    with pytest.raises(L.LedgerError, match="view band entries are always exploratory"):
        ledger.append("hindcast/v1/v1-run-1/claims.jsonl", L.claim(kind="hindcast", run_id="v1-run-1", **key))
    assert not (ledger.root / LIVE).exists()

    claim_id = L.slot_id(ledger.name, "live", "v1", VIEW, MONDAY.isoformat())
    for entry in (
        L.score(ledger=ledger.name, claim_id=claim_id, kind="live", bundle="v1", vintage=FINAL, outcome="held"),
        L.score(ledger=ledger.name, claim_id=claim_id.replace(VIEW, B.DISTRICT), kind="live", bundle="v1",
                vintage=FINAL, outcome="held", band=VIEW),
        L.correction(ledger=ledger.name, target_id=claim_id, kind="live", bundle="v1", seq=1, action="void",
                     reason="test"),
    ):
        with pytest.raises(L.LedgerError, match="view band entries are always exploratory"):
            L.validate({**entry, "recorded_at": "2026-10-19T10:30:00Z"})


# --- scoring -----------------------------------------------------------------


def issue_on(lg: L.Ledger, day: date, **options) -> list[dict]:
    fc.write_record(lg.root, uneven_record(day))
    at_issue = L.Ledger(lg.root, clock=lambda: datetime(day.year, day.month, day.day, 10, 30, tzinfo=UTC))
    return issue(at_issue, day, **options)


def test_the_views_claim_is_scored_by_the_same_rule_against_its_own_cells(tmp_path):
    lg = add_view(scoring_ledger(tmp_path / "mandi-wheat"))
    district, view = issue_on(lg, MONDAY)
    save_rain(lg.root, WINDOW_FIRST, week(10.0), REALTIME)

    for_district, for_view = scoring.score(lg, EXPLORATORY, vintage=REALTIME, code=CODE)

    assert (for_view["claim_id"], for_view["kind"], for_view["band"]) == (view["id"], "exploratory", VIEW)
    assert (for_view["outcome"], for_view["probability"]) == ("held", view["probability"])
    assert (for_view["timing_test"], for_view["timing"]) == ("forecast_availability", "on_time")
    assert VIEW_PATH in for_view["evidence"] and "bands/band-map.csv" not in for_view["evidence"]
    assert "bands/band-map.csv" in for_district["evidence"] and VIEW_PATH not in for_district["evidence"]
    assert for_district["claim_id"] == district["id"]
    assert "brier" not in " ".join(for_view)
    assert scoring.check(lg, EXPLORATORY) == []
    assert scoring.score(lg, EXPLORATORY, vintage=REALTIME, code=CODE) == []

    forged = [{**e, "outcome": "not_held"} if e["id"] == for_view["id"] else e
              for e in lg.read("scores/exploratory.jsonl")]
    (lg.root / "scores/exploratory.jsonl").write_bytes(b"".join(L.encode(e) for e in forged))
    assert scoring.check(lg, EXPLORATORY) == [f"{for_view['id']}: does not re-derive from its evidence files"]


def test_a_district_score_is_unchanged_by_the_views_presence(tmp_path):
    plain = scoring_ledger(tmp_path / "plain/mandi-wheat")
    viewed = add_view(scoring_ledger(tmp_path / "viewed/mandi-wheat"))
    for lg in (plain, viewed):
        issue_on(lg, MONDAY)
        save_rain(lg.root, WINDOW_FIRST, week(10.0), REALTIME)
    [alone] = scoring.score(plain, EXPLORATORY, vintage=REALTIME, code=CODE)
    beside, _ = scoring.score(viewed, EXPLORATORY, vintage=REALTIME, code=CODE)
    assert beside == alone


def test_standing_never_counts_a_view_even_under_a_tagged_bundle(tmp_path):
    days = [date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)]
    results = {}
    for name in ("plain", "viewed"):
        lg = with_v1(scoring_ledger(tmp_path / name / "mandi-wheat", datetime(2027, 6, 1, tzinfo=UTC)))
        if name == "viewed":
            add_view(lg)
        lg.append("tags.jsonl", L.tag(ledger=lg.name, name="mandi-wheat/bundle-v1", commit="c0de" * 10))
        ids = [issue_on(lg, day, kind="live", bundle="v1")[0]["id"] for day in days]
        save_rain(lg.root, WINDOW_FIRST, [12.0] + [0.0] * 20, "final-r20270601")
        scoring.score(lg, LIVE, vintage="final-r20270601", code=CODE,
                      attested={i: datetime(2026, 10, 19, 11, tzinfo=UTC) for i in ids})
        scoring.score(lg, EXPLORATORY, vintage="final-r20270601", code=CODE)
        results[name] = lg

    viewed = results["viewed"]
    assert [e["band"] for e in viewed.read("scores/exploratory.jsonl")] == [VIEW] * 3
    # The registered verdict reads the same with and without the view.
    assert scoring.standing(viewed, "scores/live.jsonl", "v1") == scoring.standing(
        results["plain"], "scores/live.jsonl", "v1")
    # The view's own scores give no skill, episode count or cost-loss value, tag or no tag.
    held_back = scoring.standing(viewed, "scores/exploratory.jsonl", "v1")
    assert (held_back.skill.bss, held_back.skill.rows, held_back.skill.clusters) == (None, 0, 0)
    assert (held_back.outcomes, held_back.episodes, held_back.economic_values) == ({}, {}, {})
    assert scoring.registered(viewed.read("scores/exploratory.jsonl")) == []


def test_the_commands_issue_score_and_check_the_view_and_print_no_brier_or_skill(tmp_path, monkeypatch, capsys):
    lg = add_view(scoring_ledger(tmp_path / "ledger/mandi-wheat"))
    fc.write_record(lg.root, uneven_record())
    save_rain(lg.root, WINDOW_FIRST, week(10.0), REALTIME)
    monkeypatch.setattr(cli, "_git", lambda repo, *args: (CODE + "\n").encode())
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "Ledger", lambda root: L.Ledger(root, clock=lambda: AFTER_WINDOW))
    repo = ["--repo", str(tmp_path)]

    assert cli.main(["issue", "--date", "2026-10-19", *repo]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "evidence issue: claim mandi-wheat:exploratory:dev:district:2026-10-19: "
        "≥10 mm rain in band district within 7 days: 57%",
        f"evidence issue: claim mandi-wheat:exploratory:dev:{VIEW}:2026-10-19: "
        f"≥10 mm rain in band {VIEW} within 7 days: 1%",
    ]
    assert cli.main(["issue", "--check", *repo]) == 0
    capsys.readouterr()

    assert cli.main(["score", "--vintage", REALTIME, *repo]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"evidence score: mandi-wheat:exploratory:dev:district:2026-10-19@{REALTIME}: held (provisional)",
        f"evidence score: mandi-wheat:exploratory:dev:{VIEW}:2026-10-19@{REALTIME}: held (provisional)",
        "evidence score: exploratory current scores: 1 held, 0 not_held, 0 unverifiable",
        f"evidence score: exploratory current scores, {VIEW} (a view, never part of a verdict): "
        "1 held, 0 not_held, 0 unverifiable",
    ]
    assert "brier" not in out.lower() and "skill" not in out.lower()
    assert cli.main(["score", "--check", *repo]) == 0


# --- the committed view ------------------------------------------------------


def test_the_committed_view_is_the_foothill_cell_alone_with_the_registered_maps_row():
    registered, *found = views.areas(REAL_LEDGER)
    [view] = found
    [cell] = view.band_map.cells
    [same] = [c for c in registered.band_map.cells if (c.lat, c.lon) == (32.0, 76.75)]
    assert view.path.name == f"{FOOTHILL}.csv"
    assert view.band_map.verdict_bands == (FOOTHILL,)
    assert (cell.lat, cell.lon, cell.district_km2, cell.weight) == (32.0, 76.75, same.district_km2, 1.0)
    assert view.path.read_bytes() == (
        b"lat,lon,district_km2,weight,verdict_band,reason\n32.00,76.75,356.850,1.000000,view-foothill-cell,\n")
    assert registered.band_map.verdict_bands == (B.DISTRICT,)


def test_the_findings_cell_against_district_figures_come_from_the_committed_observations():
    """Observations only: `docs/findings/2026-10-foothill-cell-exploratory-view.md` quotes these."""
    sem = semantics.load(REAL_LEDGER / "bundles/dev/bundle.toml")
    series = observations.load(REAL_LEDGER, FINAL)
    years = [y for y in observations.full_years(series) if y >= 1991]
    by_band = views.by_band(REAL_LEDGER)
    district, cell = (obs_core.band_daily(series, by_band[band].band_map, band, 1.0) for band in (B.DISTRICT, FOOTHILL))

    def mean_mm(band, month, day, days):
        totals = [obs_core.window_total(band, obs_core.run_dates(date(y, month, day), days(y))) for y in years]
        return round(sum(totals) / len(totals), 1)

    def year_days(y):
        return (date(y + 1, 1, 1) - date(y, 1, 1)).days

    windows = [obs_core.imd_dates(semantics.schedule(sem, issue))
               for y in years for issue in semantics.issue_dates(sem, y)]
    held = {name: [obs_core.outcome(obs_core.window_total(band, w), 10.0) == obs_core.HELD for w in windows]
            for name, band in (("cell", cell), ("district", district))}

    assert (years[0], years[-1], len(windows)) == (1991, 2025, 159)
    assert (mean_mm(cell, 1, 1, year_days), mean_mm(district, 1, 1, year_days)) == (1833.2, 1206.5)
    assert (mean_mm(cell, 10, 15, lambda y: 32), mean_mm(district, 10, 15, lambda y: 32)) == (22.7, 18.5)
    assert (sum(held["cell"]), sum(held["district"])) == (21, 18)
    assert sum(a == b for a, b in zip(held["cell"], held["district"])) == 152
    assert sum(a and not b for a, b in zip(held["cell"], held["district"])) == 5


def test_the_committed_band_files_have_the_hashes_their_provenance_records():
    for name, provenance in (("band-map.csv", "PROVENANCE.md"),
                             (f"{FOOTHILL}.csv", f"{FOOTHILL}.PROVENANCE.md")):
        digest = hashlib.sha256((REAL_LEDGER / "bands" / name).read_bytes()).hexdigest()
        assert f"`{name}` SHA-256: `{digest}`" in (REAL_LEDGER / "bands" / provenance).read_text()

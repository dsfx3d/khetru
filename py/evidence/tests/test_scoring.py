"""U7: the score core, the verdict quantities and the score driver. Inputs are synthetic but for one pipeline check."""

import dataclasses
import shutil
from datetime import UTC, date, datetime, timedelta

import pytest

from claim_fixtures import CODE, DEV_BUNDLE, MONDAY
from khetru_evidence import bands as B
from khetru_evidence import claim_core as C
from khetru_evidence import claims, cli, forecasts as fc, obs_core as O, observations, score_core as S, scoring, semantics
from khetru_evidence import ledger as L
from score_fixtures import (
    AFTER_WINDOW, CLIMATOLOGY, FINAL, REALTIME, WINDOW_FIRST, issue, save_rain, scoring_ledger, week,
)

EXPLORATORY, SCORES = "claims/exploratory.jsonl", "scores/exploratory.jsonl"
P_CLAIM = round(30.5 / 52, 6)


@pytest.fixture
def sem():
    return semantics.load(DEV_BUNDLE / "bundle.toml")


@pytest.fixture
def rules():
    return S.load_rules(DEV_BUNDLE / "scoring.toml")


@pytest.fixture
def ledger(tmp_path):
    return scoring_ledger(tmp_path / "mandi-wheat")


def score(lg, vintage=REALTIME, rel=EXPLORATORY, **options):
    return scoring.score(lg, rel, vintage=vintage, code=CODE, **options)


def scored(outcome, probability, climatology=0.3, issue_date="2027-10-18", band="district"):
    """A score with only the fields the verdict quantities read."""
    return {"outcome": outcome, "probability": probability, "climatology_probability": climatology,
            "issue_date": issue_date, "band": band}


# --- one claim, one vintage --------------------------------------------------


def test_a_held_claim_at_60_percent_scores_brier_016_and_033_better_than_climatology_at_30():
    claim, climatology = S.brier_pair(scored(O.HELD, 0.6))
    assert claim == pytest.approx(0.16)
    assert climatology - claim == pytest.approx(0.33)
    assert S.brier_pair(scored(O.UNVERIFIABLE, 0.6)) is None


def test_a_claim_is_scored_on_its_own_probability_against_the_observed_window(ledger):
    claim = issue(ledger)
    save_rain(ledger.root, WINDOW_FIRST, week(10.0), REALTIME)

    [entry] = score(ledger)

    assert entry["id"] == f"{claim['id']}@{REALTIME}"
    assert (entry["claim_id"], entry["kind"], entry["bundle"], entry["code"]) == (claim["id"], "exploratory", "dev", CODE)
    assert (entry["outcome"], entry["observed_mm"], entry["provisional"]) == ("held", 10.0, True)
    assert entry["observed_daily_mm"] == [10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert (entry["scored_as"], entry["probability"]) == ("claim", P_CLAIM)
    assert (entry["climatology_probability"], entry["climatology_events"], entry["climatology_windows"]) == (
        CLIMATOLOGY, 15, 45)
    assert (entry["timing_test"], entry["timing"]) == ("forecast_availability", "on_time")
    assert (entry["window_start"], entry["window_end"]) == (claim["window_start"], claim["window_end"])
    assert sorted(entry["evidence"]) == [
        "bands/band-map.csv", "bundles/dev/bundle.toml", "bundles/dev/claims.toml",
        "bundles/dev/observations.toml", "bundles/dev/scoring.toml",
        "observations/imd-final-r20261010-20231001-20231130.json.gz",
        "observations/imd-final-r20261010-20241001-20241130.json.gz",
        "observations/imd-final-r20261010-20251001-20251130.json.gz",
        "observations/imd-realtime-r20261028-20261021-20261027.json.gz",
    ]
    assert "brier" not in " ".join(entry)


def test_a_window_just_short_of_the_threshold_did_not_hold(ledger):
    issue(ledger)
    save_rain(ledger.root, WINDOW_FIRST, week(9.999), REALTIME)
    [entry] = score(ledger)
    assert (entry["outcome"], entry["observed_mm"]) == ("not_held", 9.999)


def test_the_claims_own_season_is_left_out_of_its_climatology(tmp_path):
    lg = scoring_ledger(tmp_path / "mandi-wheat")
    save_rain(lg.root, date(2026, 10, 1), [20.0] * 61, FINAL)
    issue(lg)
    [entry] = score(lg, FINAL)
    assert (entry["climatology_events"], entry["climatology_windows"], entry["provisional"]) == (15, 45, False)


def test_abstain_not_issued_voided_and_late_claims_each_score_a_brier_difference_of_exactly_0(tmp_path, sem):
    lg = scoring_ledger(tmp_path / "mandi-wheat", datetime(2026, 11, 12, tzinfo=UTC))
    issue(lg, abstain_if_missing=True)
    voided = issue(lg, date(2026, 10, 26))
    lg.append(EXPLORATORY, L.correction(ledger=lg.name, target_id=voided["id"], kind="exploratory",
                                        bundle="dev", seq=1, action="void", reason="test"))
    claims.backfill_missing(lg, 2026, bundle="dev", kind="exploratory")
    save_rain(lg.root, WINDOW_FIRST, [10.0] + [0.0] * 20, "realtime-r20261112")

    entries = score(lg, "realtime-r20261112")

    assert [(e["issue_date"], e["scored_as_reason"]) for e in entries] == [
        ("2026-10-19", "abstain"), ("2026-10-26", "voided"), ("2026-11-02", "not_issued")]
    late = S.score_claim(
        sem=sem, threshold_mm=10.0, slot=voided, voided=False, band={}, vintage=REALTIME,
        climatology=O.Climatology(0.3, 3, 10), timing_test=S.OPENTIMESTAMPS, timing_result=S.LATE, evidence={})
    for e in [*entries, late]:
        assert (e["scored_as"], e["probability"]) == ("climatology", e["climatology_probability"])
    for e in entries:
        claim, climatology = S.brier_pair(e)
        assert claim - climatology == 0
    assert [e["outcome"] for e in entries] == ["held", "not_held", "not_held"]
    assert "timing_test" not in entries[0] and "timing_test" not in entries[2]
    assert (late["scored_as_reason"], late["outcome"]) == ("late", "unverifiable")


def test_a_live_claim_attested_after_window_start_is_late_and_before_it_is_on_time(tmp_path, sem):
    lg = scoring_ledger(tmp_path / "mandi-wheat")
    shutil.copytree(lg.root / "bundles/dev", lg.root / "bundles/v1")
    claim = issue(lg, kind="live", bundle="v1")
    save_rain(lg.root, WINDOW_FIRST, week(10.0), REALTIME)
    window_start = semantics.schedule(sem, MONDAY).window_start

    assert S.timing(sem, claim, S.OPENTIMESTAMPS) == "pending"
    assert S.timing(sem, claim, S.OPENTIMESTAMPS, window_start - timedelta(seconds=1)) == "on_time"
    assert S.timing(sem, claim, S.OPENTIMESTAMPS, window_start) == "late"

    # Until a proof says when the claim was attested, its score waits.
    assert score(lg, rel="claims/live.jsonl") == []
    [entry] = score(lg, rel="claims/live.jsonl", attested={claim["id"]: window_start + timedelta(hours=1)})
    assert (entry["timing_test"], entry["timing"]) == ("opentimestamps", "late")
    assert (entry["scored_as"], entry["scored_as_reason"], entry["kind"]) == ("climatology", "late", "live")


def test_a_hindcast_claim_with_no_stamp_is_scored_on_its_forecast_when_its_run_was_available(sem):
    body = claims.bodies(DEV_BUNDLE.parents[1], claims.Bundle(DEV_BUNDLE.parents[1], "dev"), MONDAY, None)[0]
    claim = {**body, "id": "c", "type": "claim", "probability": 0.6, "run_init": "2026-10-19T00:00:00Z"}
    assert S.timing(sem, claim, S.FORECAST_AVAILABILITY) == "on_time"
    assert S.timing(sem, {**claim, "run_init": "2026-10-19T12:00:00Z"}, S.FORECAST_AVAILABILITY) == "late"

    entry = S.score_claim(
        sem=sem, threshold_mm=10.0, slot=claim, voided=False, band={}, vintage=FINAL,
        climatology=O.Climatology(0.3, 3, 10), timing_test=S.FORECAST_AVAILABILITY, timing_result="on_time",
        evidence={})
    assert (entry["scored_as"], entry["probability"]) == ("claim", 0.6)
    with pytest.raises(S.ScoreError, match="waits"):
        S.score_claim(sem=sem, threshold_mm=10.0, slot=claim, voided=False, band={}, vintage=FINAL,
                      climatology=O.Climatology(0.3, 3, 10), timing_test=S.OPENTIMESTAMPS,
                      timing_result="pending", evidence={})


def test_a_claim_whose_threshold_is_not_the_bundles_is_refused(ledger, sem):
    claim = issue(ledger)
    with pytest.raises(S.ScoreError, match="not the bundle's"):
        S.score_claim(sem=sem, threshold_mm=5.0, slot=claim, voided=False, band={}, vintage=FINAL,
                      climatology=O.Climatology(0.3, 3, 10), timing_test=S.FORECAST_AVAILABILITY,
                      timing_result="on_time", evidence={})


def test_a_day_with_no_value_makes_the_outcome_unverifiable_and_it_is_counted_but_not_scored(ledger):
    issue(ledger)
    save_rain(ledger.root, WINDOW_FIRST, week(None), REALTIME)

    [entry] = score(ledger)

    assert (entry["outcome"], entry["observed_mm"], entry["observed_daily_mm"][0]) == ("unverifiable", None, None)
    assert S.rows([entry, scored(O.HELD, 0.6)]) == [S.Row("2027-10-18", pytest.approx(0.16), pytest.approx(0.49))]
    assert scoring.outcomes([entry]) == {"unverifiable": 1}
    assert S.contingency([entry], 0.2) == (0, 0, 0, 0)


def test_a_saved_rabi_2026_run_scores_against_the_committed_observations(sem, rules):
    """Pipeline check only (KTD11): real files go through both cores end to end; no skill is computed."""
    root = DEV_BUNDLE.parents[1]
    realtime = observations.load(root, "realtime-r20261010")
    day = date(2026, 10, 1)  # the first saved run; its window is IMD's 3 to 9 Oct
    any_day = dataclasses.replace(sem, issue_weekday=day.weekday(), season_start=(day.month, day.day))
    bundle, band_map = claims.Bundle(root, "dev"), observations.load_band_map(root)
    saved = root / fc.record_path("opendata", semantics.schedule(any_day, day).run_init)
    claim = claims.entry(
        C.make_claim(sem=any_day, rule=bundle.rule, coverage_share=bundle.coverage_share, band_map=band_map,
                     verdict_band=B.DISTRICT, issue_date=day, record=fc.decode_record(saved.read_bytes()),
                     evidence={}),
        ledger="mandi-wheat", kind="exploratory", bundle="dev", code=CODE)
    final = observations.load(root, rules.climatology_vintage)
    window = O.imd_dates(semantics.schedule(any_day, day))
    climatology = O.climatology(
        O.band_daily(final, band_map, B.DISTRICT, bundle.coverage_share), window[0],
        years=observations.full_years(final), days=len(window), threshold_mm=bundle.rule.threshold_mm,
        half_window_days=bundle.observations.climatology_half_window_days,
        pseudo_count=bundle.observations.climatology_pseudo_count)

    entry = S.score_claim(
        sem=any_day, threshold_mm=bundle.rule.threshold_mm, slot=claim, voided=False,
        band=O.band_daily(realtime, band_map, B.DISTRICT, bundle.coverage_share), vintage=realtime.vintage,
        climatology=climatology, timing_test=S.FORECAST_AVAILABILITY,
        timing_result=S.timing(any_day, claim, S.FORECAST_AVAILABILITY), evidence={})

    assert entry["outcome"] in ("held", "not_held")
    assert (entry["scored_as"], entry["provisional"], entry["timing"]) == ("claim", True, "on_time")
    assert len(entry["observed_daily_mm"]) == 7 and None not in entry["observed_daily_mm"]
    assert entry["climatology_windows"] == 35 * 15


# --- the driver: waiting, re-scoring, re-deriving ----------------------------


def test_a_slot_waits_while_its_window_is_open_or_the_vintage_lacks_a_date(tmp_path):
    lg = scoring_ledger(tmp_path / "mandi-wheat", datetime(2026, 10, 27, 2, 59, tzinfo=UTC))
    issue(lg)
    save_rain(lg.root, WINDOW_FIRST, week(10.0), REALTIME)
    assert score(lg) == []

    closed = L.Ledger(lg.root, clock=lambda: AFTER_WINDOW)
    save_rain(lg.root, WINDOW_FIRST, [10.0] * 6, "realtime-r20261026")
    assert score(closed, "realtime-r20261026") == []
    assert not (lg.root / SCORES).exists()
    assert [e["outcome"] for e in score(closed)] == ["held"]
    with pytest.raises(O.ObservationError, match="no saved records"):
        score(closed, "realtime-r20270101")


def test_scoring_the_same_claim_and_vintage_twice_writes_nothing_the_second_time(ledger):
    issue(ledger)
    save_rain(ledger.root, WINDOW_FIRST, week(10.0), REALTIME)
    score(ledger)
    before = (ledger.root / SCORES).read_bytes()
    assert score(ledger) == []
    assert (ledger.root / SCORES).read_bytes() == before


def test_a_provisional_held_that_final_data_refutes_gets_a_superseding_score_with_its_reason(tmp_path):
    lg = scoring_ledger(tmp_path / "mandi-wheat", datetime(2027, 6, 1, tzinfo=UTC))
    issue(lg)
    save_rain(lg.root, WINDOW_FIRST, week(10.0), REALTIME)
    [provisional] = score(lg)
    original = (lg.root / SCORES).read_bytes()
    save_rain(lg.root, WINDOW_FIRST, week(4.0), "final-r20270601")

    [final] = score(lg, "final-r20270601")

    assert (provisional["outcome"], final["outcome"], final["provisional"]) == ("held", "not_held", False)
    assert final["supersedes"] == provisional["id"]
    assert final["reason"] == f"final-r20270601 observations replace {REALTIME}: outcome held -> not_held"
    assert (lg.root / SCORES).read_bytes().startswith(original)
    assert lg.check_file(SCORES) == []
    assert scoring.current(lg.read(SCORES)) == [final]
    assert scoring.outcomes(lg.read(SCORES)) == {"not_held": 1}
    # Final observations are never replaced by real-time ones, nor a retrieval by an earlier one.
    save_rain(lg.root, WINDOW_FIRST, week(10.0), "realtime-r20270602")
    save_rain(lg.root, WINDOW_FIRST, week(10.0), "final-r20270101")
    assert score(lg, "realtime-r20270602") == score(lg, "final-r20270101") == score(lg) == []


def test_every_score_re_derives_from_the_files_it_names(ledger):
    issue(ledger)
    save_rain(ledger.root, WINDOW_FIRST, week(10.0), REALTIME)
    [entry] = score(ledger)
    assert scoring.check(ledger, EXPLORATORY) == []

    (ledger.root / SCORES).write_bytes(L.encode({**entry, "outcome": "not_held"}))
    assert scoring.check(ledger, EXPLORATORY) == [f"{entry['id']}: does not re-derive from its evidence files"]

    (ledger.root / SCORES).write_bytes(L.encode(entry))
    (ledger.root / "bundles/dev/scoring.toml").write_text(
        (DEV_BUNDLE / "scoring.toml").read_text().replace("min_episodes = 12", "min_episodes = 2"))
    assert scoring.check(ledger, EXPLORATORY) == [
        f"{entry['id']}: evidence file bundles/dev/scoring.toml is missing or changed"]


def test_the_score_command_prints_outcomes_and_counts_but_no_brier_or_skill(tmp_path, monkeypatch, capsys):
    lg = scoring_ledger(tmp_path / "ledger/mandi-wheat")
    issue(lg)
    save_rain(lg.root, WINDOW_FIRST, week(10.0), REALTIME)
    monkeypatch.setattr(cli, "_git", lambda repo, *args: (CODE + "\n").encode())
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "Ledger", lambda root: L.Ledger(root, clock=lambda: AFTER_WINDOW))
    args = ["score", "--repo", str(tmp_path)]

    assert cli.main([*args, "--vintage", REALTIME]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"evidence score: mandi-wheat:exploratory:dev:district:2026-10-19@{REALTIME}: held (provisional)",
        "evidence score: exploratory current scores: 1 held, 0 not_held, 0 unverifiable",
    ]
    assert cli.main([*args, "--check"]) == 0
    assert cli.main([*args, "--vintage", REALTIME, "--kind", "live"]) == 2
    assert "never written from a local machine" in capsys.readouterr().err


# --- skill, bootstrap and verdicts -------------------------------------------


def seasons(first_year, count, briers, climatology=0.2):
    """``count`` seasons of five issue dates; Brier scores cycle through ``briers``."""
    dates = [f"{first_year + s}-10-{15 + 7 * i:02d}" for s in range(count) for i in range(3)]
    dates += [f"{first_year + s}-11-{5 + 7 * i:02d}" for s in range(count) for i in range(2)]
    return [S.Row(d, briers[i % len(briers)], climatology) for i, d in enumerate(sorted(dates))]


def issue_dates(count, briers, climatology=0.2):
    first = date(2027, 10, 18)
    return [S.Row((first + timedelta(days=7 * i)).isoformat(), briers[i % len(briers)], climatology)
            for i in range(count)]


def test_brier_scores_are_averaged_across_bands_per_issue_date():
    rows = S.rows([scored(O.HELD, 0.6, band="a"), scored(O.NOT_HELD, 0.6, band="b"),
                   scored(O.NOT_HELD, 0.1, issue_date="2027-10-25")])
    assert rows == [S.Row("2027-10-18", pytest.approx(0.26), pytest.approx(0.29)),
                    S.Row("2027-10-25", pytest.approx(0.01), pytest.approx(0.09))]
    assert S.bss(rows) == pytest.approx(1 - 0.27 / 0.38)
    assert S.bss([]) is None


def test_the_bootstrap_repeats_for_a_seed_and_differs_for_another(rules):
    rows = seasons(2006, 20, [0.02, 0.18, 0.1, 0.07, 0.13, 0.05, 0.11])
    again = S.skill(rows, S.season, rules)
    assert S.skill(rows, S.season, rules) == again
    assert (again.clusters, again.rows) == (20, 100)
    assert again.lower < again.bss < again.upper
    other = S.skill(rows, S.season, dataclasses.replace(rules, bootstrap_seed=rules.bootstrap_seed + 1))
    assert (other.lower, other.upper) != (again.lower, again.upper)
    assert other.bss == again.bss


def test_perfect_forecasts_over_20_seasons_pass_and_forecasts_equal_to_climatology_fail(rules):
    held = [scored(O.HELD if i % 3 == 0 else O.NOT_HELD, 0.0, issue_date=r.issue_date)
            for i, r in enumerate(seasons(2006, 20, [0.0]))]
    perfect = [{**s, "probability": 0.99 if s["outcome"] == O.HELD else 0.01} for s in held]
    calendar = [{**s, "probability": s["climatology_probability"]} for s in held]

    sharp = S.skill(S.rows(perfect), S.season, rules)
    assert sharp.lower > 0.99
    assert S.hindcast_verdict(sharp, 20, 20, rules) == "pass"
    flat = S.skill(S.rows(calendar), S.season, rules)
    assert (flat.bss, flat.lower, flat.upper) == (0, 0, 0)
    assert S.hindcast_verdict(flat, 20, 20, rules) == "fail"
    # Too few independent episodes of either sign is too early to tell, whatever the skill.
    assert S.hindcast_verdict(sharp, 20, 11, rules) == S.hindcast_verdict(sharp, 11, 20, rules) == "too_early_to_tell"
    assert S.hindcast_verdict(S.skill([], S.season, rules), 20, 20, rules) == "too_early_to_tell"


def test_a_band_with_4_live_episodes_and_a_minimum_of_12_is_too_early_even_when_every_claim_held(rules):
    assert rules.min_episodes == 12
    every_claim_held = [scored(O.HELD, 0.99, issue_date=r.issue_date) for r in issue_dates(4, [0.0])]
    result = S.skill(S.rows(every_claim_held), S.issue_date, rules)
    assert result.lower > 0.9
    assert S.band_switch(result, 4, 0, [1.0], rules) == "too_early_to_tell"
    assert S.band_switch(result, 12, 12, [0.4, 0.1], rules) == "pass"
    # Skill alone does not earn sharp calls: the cost-loss value must be above zero at every ratio.
    assert S.band_switch(result, 12, 12, [0.4, 0.0], rules) == "fail"
    assert S.band_switch(result, 12, 12, [0.4, None], rules) == "fail"


def test_live_skill_equal_to_hindcast_skill_passes_with_many_issue_dates_and_is_too_early_with_4(rules):
    hindcast_rows = seasons(2006, 20, [0.02, 0.18])
    hindcast = S.skill(hindcast_rows, S.season, rules)
    assert hindcast.lower > rules.noninferiority_margin

    many = S.skill_difference(hindcast_rows, issue_dates(200, [0.02, 0.18]), rules)
    assert many.bss == pytest.approx(0)
    assert S.live_check(hindcast, many, rules) == "pass"
    few = S.skill_difference(hindcast_rows, issue_dates(4, [0.02, 0.18]), rules)
    assert few.bss == pytest.approx(0)
    assert S.live_check(hindcast, few, rules) == "too_early_to_tell"
    worse = S.skill_difference(hindcast_rows, issue_dates(200, [0.19, 0.21]), rules)
    assert S.live_check(hindcast, worse, rules) == "fail"
    assert S.live_check(hindcast, S.skill_difference(hindcast_rows, [], rules), rules) == "too_early_to_tell"


def test_a_hindcast_lower_bound_at_or_below_the_margin_keeps_the_live_check_too_early(rules):
    weak_rows = seasons(2006, 20, [0.19])
    weak = S.skill(weak_rows, S.season, rules)
    assert 0 < weak.lower <= rules.noninferiority_margin
    matching = S.skill_difference(weak_rows, issue_dates(200, [0.19]), rules)
    assert matching.upper < rules.noninferiority_margin
    assert S.live_check(weak, matching, rules) == "too_early_to_tell"


def test_the_gates_open_on_a_hindcast_pass_unless_the_live_check_fails():
    assert S.gates("pass", "pass") == ("pass", False)
    assert S.gates("pass", "too_early_to_tell") == ("pass", True)
    assert S.gates("pass", "fail") == S.gates("fail", "pass") == ("fail", False)
    assert S.gates("too_early_to_tell", "too_early_to_tell") == ("too_early_to_tell", False)


def test_cost_loss_value_at_02_matches_richardsons_formula_by_hand():
    table = ([scored(O.HELD, 0.5)] * 30 + [scored(O.NOT_HELD, 0.5)] * 10
             + [scored(O.HELD, 0.1)] * 10 + [scored(O.NOT_HELD, 0.1)] * 50)
    assert S.contingency(table, 0.2) == (30, 10, 10, 50)
    # Base rate 0.4. Expense per case with loss 1 and cost 0.2: forecast 0.4 * 0.2 + 0.1 = 0.18;
    # climatology min(0.2, 0.4) = 0.2; perfect 0.4 * 0.2 = 0.08. Value (0.2 - 0.18) / (0.2 - 0.08).
    assert S.economic_value(30, 10, 10, 50, 0.2) == pytest.approx(1 / 6)
    assert S.economic_value(0, 10, 0, 50, 0.2) is None
    assert S.economic_value(0, 0, 0, 0, 0.2) is None


# --- reading scores back -----------------------------------------------------


def test_skill_is_held_back_for_a_bundle_with_no_recorded_tag(ledger):
    issue(ledger)
    save_rain(ledger.root, WINDOW_FIRST, week(10.0), REALTIME)
    score(ledger)
    with pytest.raises(S.ScoreError, match="held back"):
        scoring.standing(ledger, SCORES, "dev")


def test_standing_reads_skill_episodes_and_cost_loss_from_a_tagged_bundles_scores(tmp_path):
    lg = scoring_ledger(tmp_path / "mandi-wheat", datetime(2027, 6, 1, tzinfo=UTC))
    shutil.copytree(lg.root / "bundles/dev", lg.root / "bundles/v1")
    lg.append("tags.jsonl", L.tag(ledger=lg.name, name="mandi-wheat/bundle-v1", commit="c0de" * 10))
    days = [date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)]
    ids = [issue(lg, day, kind="live", bundle="v1")["id"] for day in days]
    # Rain on 21 Oct only: the first window holds, and the next two are one dry run.
    save_rain(lg.root, WINDOW_FIRST, [12.0] + [0.0] * 20, "final-r20270601")
    scoring.score(lg, "claims/live.jsonl", vintage="final-r20270601", code=CODE,
                  attested={i: datetime(2026, 10, 19, 11, tzinfo=UTC) for i in ids[:1]}
                  | {i: datetime(2027, 1, 1, tzinfo=UTC) for i in ids[1:2]}
                  | {ids[2]: datetime(2026, 11, 2, 11, tzinfo=UTC)})

    result = scoring.standing(lg, "scores/live.jsonl", "v1")

    assert result.outcomes == {"held": 1, "not_held": 2}
    assert result.episodes == {"district": (1, 1)}
    # 26 Oct was late, so it is scored as climatology and still counts as a row.
    briers = [(P_CLAIM - 1) ** 2, CLIMATOLOGY ** 2, P_CLAIM ** 2]
    reference = [(CLIMATOLOGY - 1) ** 2, CLIMATOLOGY ** 2, CLIMATOLOGY ** 2]
    assert result.skill.bss == pytest.approx(1 - sum(briers) / sum(reference))
    assert (result.skill.clusters, result.skill.rows) == (3, 3)
    assert set(result.economic_values["district"]) == {0.08, 0.2, 0.34}
    assert S.contingency(scoring.current(lg.read("scores/live.jsonl")), 0.34) == (1, 1, 0, 1)

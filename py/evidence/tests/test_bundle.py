"""U8: the power simulation. Rainfall is synthetic; the committed IMD files are not read here."""

import dataclasses
import hashlib
import shutil
from datetime import date, timedelta

import numpy as np
import pytest

from khetru_evidence import bands as B
from khetru_evidence import bundle, cli, obs_core as O, observations, score_core as S, semantics
from test_observations import DEV_BUNDLE, synthetic_ledger

SMALL = bundle.Design(
    thresholds_mm=(10.0,), counted_first_years=(2002,), simulated_first_years=(2002,), min_episodes=(1, 2),
    confidences=(0.9,), margins=(0.1,), skills=(0.2, 0.5), replicates=20, bootstrap_resamples=200,
    season_cap=20,
)
YEARS = (1991, 2002, 2003)
VIEW = bundle.DECEMBER.views[0]
LEDGER_ROOT = DEV_BUNDLE.parents[1]  # the committed ledger: its bundles and its tables, never its IMD files


@pytest.fixture
def sem():
    return semantics.load(DEV_BUNDLE / "bundle.toml")


@pytest.fixture
def obs_rules():
    return O.load_rules(DEV_BUNDLE / "observations.toml")


@pytest.fixture
def rules():
    return S.load_rules(DEV_BUNDLE / "scoring.toml")


def dry_but(*wet: tuple[date, float]) -> dict[date, float]:
    """Every day of October to December of ``YEARS`` dry, but for the ``wet`` days."""
    band = {date(y, 10, 1) + timedelta(days=i): 0.0 for y in YEARS for i in range(92)}
    return band | dict(wet)


def season(positive: int, negative: int, year: int = 2000, held: int = 1, dry: int = 3) -> bundle.Season:
    outcomes = [O.HELD] * held + [O.NOT_HELD] * dry
    slots = tuple(bundle.Slot(f"{year}-10-{i + 1:02d}", o, 0.25) for i, o in enumerate(outcomes))
    return bundle.Season(year, slots, positive, negative)


def test_runs_of_held_and_of_not_held_are_counted_apart():
    H, N = O.HELD, O.NOT_HELD

    assert bundle.runs([H, H, N, N]) == (1, 1)
    assert bundle.runs([N, H, N, H, H]) == (2, 2)
    assert bundle.runs([N, N]) == (0, 1)
    assert bundle.runs([]) == (0, 0)


def test_window_event_counts_what_the_base_rate_table_counts(sem, obs_rules):
    # 2002's Mondays are 21 and 28 Oct, 4 and 11 Nov; the second window holds 1 Nov.
    seasons = bundle.observed_seasons(dry_but((date(2002, 11, 1), 12.0)), YEARS, sem, obs_rules,
                                      bundle.WINDOW, 10.0)

    wet = seasons[1]
    assert [s.outcome for s in wet.slots] == [O.NOT_HELD, O.HELD, O.NOT_HELD, O.NOT_HELD]
    assert (wet.positive, wet.negative) == (1, 2)
    assert bundle.episodes(seasons) == (1, 4)


def test_rain_before_the_cutoff_holds_for_every_issue_date_before_the_rain(sem, obs_rules):
    seasons = bundle.observed_seasons(dry_but((date(2002, 11, 8), 12.0)), YEARS, sem, obs_rules,
                                      bundle.BEFORE_CUTOFF, 10.0)

    # Windows start 23 and 30 Oct, 6 and 13 Nov: the first three can still reach 8 Nov.
    wet = seasons[1]
    assert [s.outcome for s in wet.slots] == [O.HELD, O.HELD, O.HELD, O.NOT_HELD]
    assert (wet.positive, wet.negative) == (1, 1)
    assert [(s.positive, s.negative) for s in seasons] == [(0, 1), (1, 1), (0, 1)]


def test_cutoff_climatology_leaves_the_scored_season_out(sem, obs_rules):
    band = dry_but((date(2002, 11, 8), 12.0))
    seasons = bundle.observed_seasons(band, YEARS, sem, obs_rules, bundle.BEFORE_CUTOFF, 10.0)

    # 2 other seasons x 15 starts each; 2002 sees none of its own rain.
    assert {s.climatology for s in seasons[1].slots} == {0.5 / 31}
    first = bundle.cutoff_climatology(bundle.rain_before_cutoff(band, 7, 10.0, (11, 30)), date(1991, 10, 23),
                                      years=(2002, 2003), half_window_days=7, pseudo_count=0.5)
    assert (first.events, first.windows, first.probability) == (15, 30, 15.5 / 31)


def test_unverifiable_issue_dates_are_left_out(sem, obs_rules):
    band = dry_but()
    del band[date(2002, 10, 24)]  # inside the first window of 2002 only

    for event in bundle.EVENTS:
        seasons = bundle.observed_seasons(band, YEARS, sem, obs_rules, event, 10.0)
        assert [len(s.slots) for s in seasons] == [4, 3, 4]


def test_simulated_forecasts_have_the_stated_skill():
    rng = np.random.default_rng(1)
    held = rng.random(40_000) < 0.2
    slots = [bundle.Slot(f"{2000 + i // 5}-10-{i % 5 + 1:02d}", O.HELD if h else O.NOT_HELD, 0.2)
             for i, h in enumerate(held)]

    for skill, errors in ((0.1, bundle.INDEPENDENT), (0.4, bundle.INDEPENDENT), (0.1, bundle.SHARED),
                          (0.4, bundle.SHARED)):
        forecasts = bundle.draw_forecasts(rng, slots, skill, errors)
        assert all(0 < p < 1 for p in forecasts)
        assert S.bss(bundle.rows(slots, forecasts)) == pytest.approx(skill, abs=0.02)
        # Reliable: the event held about as often as forecasts near 0.5 said.
        near = [h for h, p in zip(held, forecasts) if 0.4 < p < 0.6]
        assert not near or np.mean(near) == pytest.approx(0.5, abs=0.1)


def test_shared_errors_make_a_season_all_good_or_all_bad():
    rng = np.random.default_rng(2)
    slots = [bundle.Slot(f"{2000 + i // 5}-10-{i % 5 + 1:02d}", O.HELD if i % 5 == 0 else O.NOT_HELD, 0.2)
             for i in range(10_000)]

    def spread_between_seasons(errors):
        table = bundle.rows(slots, bundle.draw_forecasts(rng, slots, 0.3, errors))
        return np.var([np.mean([r.brier for r in table[i:i + 5]]) for i in range(0, len(table), 5)])

    assert spread_between_seasons(bundle.SHARED) > 2 * spread_between_seasons(bundle.INDEPENDENT)
    # One season, shared: the not-held dates share a climatology, so they get one forecast.
    shared = bundle.draw_forecasts(rng, slots[:5], 0.3, bundle.SHARED)
    assert len({round(p, 12) for p in shared[1:]}) == 1
    with pytest.raises(ValueError, match="unknown errors"):
        bundle.draw_forecasts(rng, slots[:5], 0.3, "paired")


def test_a_simulated_skill_must_be_inside_0_and_1():
    for skill in (0.0, 1.0):
        with pytest.raises(ValueError, match="inside 0-1"):
            bundle.draw_forecasts(np.random.default_rng(1), season(1, 1).slots, skill)


def test_simulation_repeats_for_a_seed_and_tallies_every_record(rules):
    pool = [season(1, 1, year) for year in range(2000, 2020)]

    def run(seed, skill=0.5, errors=bundle.INDEPENDENT):
        return bundle.simulate(pool, pool, skill, errors, rules, SMALL, np.random.default_rng(seed))

    first = run(7)
    assert first == run(7)
    assert run(7, skill=0.1) != run(8, skill=0.1)
    assert first.replicates == 20
    assert all(sum(tally.values()) == 20 for tally in (*first.live_equal.values(), *first.live_none.values()))
    # A strong forecast over 20 seasons passes; a live season that states climatology never does.
    assert first.passes[0.9] == 20
    assert first.live_none[0.9, 0.1][S.PASS] == 0
    assert run(7, skill=0.05).passes[0.9] < 20
    assert run(7, errors=bundle.SHARED) != first


def test_seasons_until_judged_counts_both_kinds_of_episode():
    rng = np.random.default_rng(1)

    assert bundle.seasons_until_judged([season(1, 1)], 4, SMALL, rng) == (4.0, 4.0)
    assert bundle.seasons_until_judged([season(2, 1)], 4, SMALL, rng) == (4.0, 4.0)
    assert bundle.seasons_until_judged([season(0, 1)], 4, SMALL, rng) is None
    median, late = bundle.seasons_until_judged([season(1, 1), season(0, 1)], 2, SMALL, rng)
    assert 2 <= median <= late <= SMALL.season_cap


def test_minimum_detectable_skill_is_the_smallest_that_reaches_the_target():
    assert bundle.minimum_detectable({0.1: 0.3, 0.2: 0.85, 0.5: 1.0}, 0.8) == 0.2
    assert bundle.minimum_detectable({0.1: 0.3, 0.2: 0.6}, 0.8) is None


def power_ledger(root):
    synthetic_ledger(root)
    (root / "bundles/dev/scoring.toml").write_bytes((DEV_BUNDLE / "scoring.toml").read_bytes())


def test_power_table_reproduces_from_the_same_files_and_seed(tmp_path):
    power_ledger(tmp_path)

    text = bundle.power(tmp_path, SMALL)

    assert text == bundle.power(tmp_path, SMALL)
    # The synthetic ledger is dry but for 12 mm on 1 Nov 2002: one held window in 12.
    assert "| 10 mm | 1991–2003 | 3 | 12 | 1 | 8% | 1 | 4 | 1 |" in text
    assert "| 10 mm | 2002–2003 | 2 | 8 | 1 | 12% | 1 | 3 | 1 |" in text
    # Rain before the cutoff: 21 and 28 Oct of 2002 hold, as in the base-rate table.
    assert "| 10 mm | 1991–2003 | 3 | 12 | 2 | 17% | 1 | 3 | 1 |" in text
    assert "No real forecast enters this table." in text
    assert text.count("#### Hindcast pass rate by true skill") == 2
    assert text.count("| 10 mm | 2002–2003 | shared | 0.9 | ") == 4  # a pass row and a live row per event


def test_power_check_fails_when_the_table_was_edited(tmp_path, capsys):
    root = tmp_path / "ledger" / observations.LEDGER
    power_ledger(root)

    assert bundle.write_power(repo=tmp_path, check=True, design=SMALL) == 1
    assert bundle.write_power(repo=tmp_path, check=False, design=SMALL) == 0
    assert bundle.write_power(repo=tmp_path, check=True, design=SMALL) == 0
    table = root / bundle.POWER_PATH
    table.write_text(table.read_text(encoding="utf-8").replace("| 10 mm |", "| 11 mm |", 1), encoding="utf-8")
    assert bundle.write_power(repo=tmp_path, check=True, design=SMALL) == 1
    assert "differs" in capsys.readouterr().err


def test_bundle_power_command_runs_the_simulation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bundle, "write_power", lambda **options: calls.append(options) or 0)

    assert cli.main(["bundle", "power", "--check", "--repo", str(tmp_path)]) == 0
    assert calls == [{"repo": tmp_path, "check": True}]


# --- the December recount: issue dates to 14 Dec, cutoff 16 Dec, the views beside the district ----


@pytest.fixture
def december(sem, obs_rules):
    return bundle.values(bundle.DECEMBER, sem, obs_rules)


def test_december_variant_changes_the_last_issue_date_and_the_cutoff_only(sem, obs_rules, december):
    late_sem, late_rules = december

    assert semantics.issue_dates(late_sem, 2002) == [
        date(2002, 10, 21), date(2002, 10, 28), date(2002, 11, 4), date(2002, 11, 11), date(2002, 11, 18),
        date(2002, 11, 25), date(2002, 12, 2), date(2002, 12, 9),
    ]
    assert semantics.issue_dates(late_sem, 2003)[-1] == date(2003, 12, 8)
    assert late_sem == dataclasses.replace(sem, season_end=(12, 14))
    assert late_rules == dataclasses.replace(obs_rules, sowing_cutoff=(12, 16))
    assert bundle.VARIANTS == {"december": bundle.DECEMBER, "december-7": bundle.DECEMBER_7}


def test_first_run_keeps_the_development_values_and_its_seeds(sem, obs_rules, rules):
    assert bundle.values(bundle.FIRST_RUN, sem, obs_rules) == (sem, obs_rules)
    assert len(semantics.issue_dates(sem, 2002)) == 4
    pools = {(e, 0): [season(1, 1)] for e in range(2)}

    first = bundle.jobs(pools, 0, rules, SMALL, bundle.FIRST_RUN)

    # The seed and the row's position: 1, band, event, threshold, range, skill, kind of errors.
    assert [job[-1] for job in first.values()] == [
        [20261008, 1, 0, e, 0, 0, k, x] for e in range(2) for k in range(2) for x in range(2)
    ]
    assert bundle.until_seed(rules, bundle.FIRST_RUN, 0, 1, 2, 3) == [20261008, 0, 0, 1, 2, 3]


def test_no_row_of_the_december_variant_shares_a_seed_with_another(rules):
    pools = {(e, 0): [season(1, 1)] for e in range(2)}
    seeds = [
        tuple(job[-1])
        for variant, areas in ((bundle.FIRST_RUN, 1), (bundle.DECEMBER, 2))
        for b in range(areas)
        for job in bundle.jobs(pools, b, rules, SMALL, variant).values()
    ] + [
        tuple(bundle.until_seed(rules, variant, b, e, 0, n))
        for variant, areas in ((bundle.FIRST_RUN, 1), (bundle.DECEMBER, 2))
        for b in range(areas) for e in range(2) for n in range(2)
    ]

    assert len(set(seeds)) == len(seeds) == 3 * 8 + 3 * 4
    # No seed is another with zeros added, which numpy would read as the same seed.
    assert len({seed[:2] for seed in seeds}) == 4


def test_an_issue_date_with_no_room_before_the_cutoff_is_left_out_of_that_event(december):
    late_sem, late_rules = december
    band = dry_but((date(2002, 12, 12), 12.0))

    seasons = bundle.observed_seasons(band, YEARS, late_sem, late_rules, bundle.BEFORE_CUTOFF, 10.0)

    # The window of 9 Dec starts on 11 Dec: no 7 dates fit by 16 Dec. That of 8 Dec 2003 runs 10 to 16 Dec.
    assert [len(s.slots) for s in seasons] == [7, 7, 8]
    assert [s.no_room for s in seasons] == [1, 1, 0]
    assert seasons[1].slots[-1].issue_date == "2002-12-02"
    # 2002 holds on every issue date that is left, so it has no negative episode.
    assert {s.outcome for s in seasons[1].slots} == {O.HELD}
    assert (seasons[1].positive, seasons[1].negative) == (1, 0)
    # Climatology pools the starts with room only: 3 to 10 Dec in 1991 (dry) and in 2002 (all held).
    assert seasons[2].slots[-1].climatology == 8.5 / 17
    # The window event keeps every issue date.
    windows = bundle.observed_seasons(band, YEARS, late_sem, late_rules, bundle.WINDOW, 10.0)
    assert [(len(s.slots), s.no_room) for s in windows] == [(8, 0)] * 3
    # What the table's header says of the two choices. 2002's issue date left out had 12 mm between its
    # window's first date and the cutoff, so a shorter run would have held; 1991's had none.
    assert bundle._no_room_rain(band, YEARS, late_sem, late_rules, 10.0) == [2002]
    assert bundle._no_room_rain(band, YEARS, late_sem, late_rules, 20.0) == []
    # The 7-day windows of 9 Dec end on 17 Dec, after the cutoff; that of 8 Dec 2003 ends on it.
    assert bundle._past_cutoff(YEARS, late_sem, late_rules) == {
        "1991-12-09": date(1991, 12, 17), "2002-12-09": date(2002, 12, 17)}
    assert windows[1].slots[-1].outcome == O.HELD


def test_every_first_run_issue_date_has_room_before_the_cutoff(sem, obs_rules):
    seasons = bundle.observed_seasons(dry_but(), YEARS, sem, obs_rules, bundle.BEFORE_CUTOFF, 10.0)
    assert [s.no_room for s in seasons] == [0, 0, 0]
    # Climatology pools starts up to the half-window after the window's own. The latest of them must
    # have room too, or a first-run "not held" would turn into "left out" with nothing saying so.
    pooled = timedelta(days=obs_rules.climatology_half_window_days)
    margins = set()
    for year in range(1991, 2026):
        for issue in semantics.issue_dates(sem, year):
            first = O.imd_dates(semantics.schedule(sem, issue))[0]
            assert bundle.has_room(first, sem.rain_days, obs_rules.sowing_cutoff), issue
            assert bundle.has_room(first + pooled, sem.rain_days, obs_rules.sowing_cutoff), issue
            margins.add(date(year, *obs_rules.sowing_cutoff) - O.run_dates(first + pooled, sem.rain_days)[-1])
    # No day to spare: the latest pooled start is 24 Nov and its run ends on the cutoff, 30 Nov.
    assert min(margins) == timedelta(0)
    assert not bundle.has_room(date(2003, 11, 25), sem.rain_days, obs_rules.sowing_cutoff)


def test_a_season_is_counted_only_with_every_date_its_windows_use(december):
    late_sem, late_rules = december
    band = dry_but()

    assert bundle.short_seasons(band, YEARS, late_sem, late_rules) == {}
    # 9 Dec's window ends on 17 Dec, and climatology pools starts 7 days either side.
    assert bundle.used_dates(late_sem, late_rules, 2002) == (date(2002, 10, 16), date(2002, 12, 24))
    del band[date(2002, 12, 24)]
    assert bundle.short_seasons(band, YEARS, late_sem, late_rules) == {2002: 1}


def view_ledger(root):
    power_ledger(root)
    view = B.BandMap(cells=(B.Cell(31.5, 77.25, 100.0, 1.0, VIEW),), reason="")
    (root / f"bands/{VIEW}.csv").write_bytes(B.encode(view))


def test_first_run_table_does_not_read_a_view(tmp_path):
    power_ledger(tmp_path)
    text = bundle.power(tmp_path, SMALL)
    view_ledger(tmp_path / "with-view")

    assert bundle.power(tmp_path / "with-view", SMALL) == text == bundle.power(tmp_path, SMALL, bundle.FIRST_RUN)
    assert "- Issue dates: each Monday from 15 Oct to 15 Nov; 7-day window; sowing cutoff 30 Nov." in text
    assert text.startswith("# Mandi wheat power simulation\n\nGenerated by `evidence bundle power`; do not edit.")
    assert "view" not in text and "December" not in text


def test_december_table_counts_the_district_and_then_the_view(tmp_path):
    view_ledger(tmp_path)

    text = bundle.power(tmp_path, SMALL, bundle.DECEMBER)

    assert text == bundle.power(tmp_path, SMALL, bundle.DECEMBER)
    assert "Generated by `evidence bundle power --variant december`; do not edit." in text
    assert "- Issue dates: each Monday from 15 Oct to 14 Dec; 7-day window; sowing cutoff 16 Dec." in text
    assert "after the first run" in text and "No real forecast enters this table." in text
    digest = hashlib.sha256((tmp_path / f"bands/{VIEW}.csv").read_bytes()).hexdigest()
    assert f"`bands/{VIEW}.csv`, SHA-256\n  `{digest}`" in text
    district, view = text.index("## Verdict band `district`"), text.index(f"## Exploratory view `{VIEW}`")
    assert district < view
    assert "## Verdict band `view" not in text
    # 8 Mondays a season; the rain of 1 Nov 2002 falls on every cell, so both areas count alike.
    assert text.count("| 10 mm | 1991–2003 | 3 | 24 | 1 | 4% | 1 | 4 | 1 |") == 2
    # Rain before the cutoff: 9 Dec of 1991 and of 2002 have no room and are left out.
    assert text.count("| 10 mm | 1991–2003 | 3 | 22 | 2 | 9% | 1 | 3 | 1 |") == 2
    assert "leaves out 2 of 24 issue dates" in text
    assert "with the development rule values except as noted below; forecasts are" in text
    # The header's figures are counted from the data. Neither issue date left out had rain before the cutoff.
    assert "| `district` | 2 | 0 |" in text and f"| `{VIEW}` | 2 | 0 |" in text
    assert "it pools the starts\n  3 to 10 Dec, 8 of 15." in text
    # Windows ending after the cutoff: those of 9 Dec 1991 and 2002 (17 Dec). The one held window is not one.
    assert "2 of 24 windows end after it, on\n  17 Dec: the last one of a season." in text
    assert text.count("| 1991–2003 | 2 | 0 of 1 |") == 2
    assert text.count("| 2002–2003 | 1 | 0 of 1 |") == 2
    assert "| `district` | 2 | 366 | 0 | 0 | 186 | 0 | 0 |" in text
    assert f"| `{VIEW}` | 1 | 183 | 0 | 0 | 93 | 0 | 0 |" in text
    assert text.count("#### Hindcast pass rate by true skill") == 4


def test_december_table_does_not_change_when_a_view_file_is_added_later(tmp_path):
    view_ledger(tmp_path)
    text = bundle.power(tmp_path, SMALL, bundle.DECEMBER)
    for later in ("view-a-earlier-name", "view-z-later-name"):
        view = B.BandMap(cells=(B.Cell(31.5, 77.0, 100.0, 1.0, later),), reason="")
        (tmp_path / f"bands/{later}.csv").write_bytes(B.encode(view))

    assert bundle.DECEMBER.views == ("view-foothill-cell",)
    assert bundle.power(tmp_path, SMALL, bundle.DECEMBER) == text


def test_december_table_is_written_beside_the_first_run_table(tmp_path, capsys):
    root = tmp_path / "ledger" / observations.LEDGER
    view_ledger(root)
    assert bundle.write_power(repo=tmp_path, check=False, design=SMALL) == 0
    first = (root / bundle.POWER_PATH).read_bytes()

    assert bundle.write_power(repo=tmp_path, check=True, design=SMALL, variant=bundle.DECEMBER) == 1
    assert bundle.write_power(repo=tmp_path, check=False, design=SMALL, variant=bundle.DECEMBER) == 0
    assert bundle.write_power(repo=tmp_path, check=True, design=SMALL, variant=bundle.DECEMBER) == 0
    assert "wrote ledger/mandi-wheat/power-december.md" in capsys.readouterr().out
    assert (root / bundle.POWER_PATH).read_bytes() == first
    assert bundle.write_power(repo=tmp_path, check=True, design=SMALL) == 0


def test_bundle_power_command_takes_a_variant(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bundle, "write_power", lambda **options: calls.append(options) or 0)

    assert cli.main(["bundle", "power", "--variant", "december", "--check", "--repo", str(tmp_path)]) == 0
    assert calls == [{"repo": tmp_path, "check": True, "variant": bundle.DECEMBER}]
    with pytest.raises(SystemExit):
        cli.main(["bundle", "power", "--variant", "january", "--repo", str(tmp_path)])
    # The choices are the module's variants: one added there is taken with no change to the command.
    january = dataclasses.replace(bundle.DECEMBER, name="january")
    monkeypatch.setitem(bundle.VARIANTS, "january", january)
    assert cli.main(["bundle", "power", "--variant", "january", "--repo", str(tmp_path)]) == 0
    assert calls[-1] == {"repo": tmp_path, "check": False, "variant": january}


# --- the recount for the owner's issue dates: every Monday to 7 Dec, cutoff 16 Dec -----------------

RUNS = ((bundle.FIRST_RUN, 1), (bundle.DECEMBER, 2), (bundle.DECEMBER_7, 2))  # each with its areas


@pytest.fixture
def december_7(sem, obs_rules):
    return bundle.values(bundle.DECEMBER_7, sem, obs_rules)


def test_december_7_variant_ends_on_the_last_monday_on_or_before_7_dec(sem, obs_rules, december_7):
    chosen_sem, chosen_rules = december_7

    assert chosen_sem == dataclasses.replace(sem, season_end=(12, 7))
    assert chosen_rules == dataclasses.replace(obs_rules, sowing_cutoff=(12, 16))
    assert semantics.issue_dates(chosen_sem, 2002) == [
        date(2002, 10, 21), date(2002, 10, 28), date(2002, 11, 4), date(2002, 11, 11), date(2002, 11, 18),
        date(2002, 11, 25), date(2002, 12, 2),
    ]
    lasts = set()
    for year in range(1991, 2026):
        issues = semantics.issue_dates(chosen_sem, year)
        last = issues[-1]
        assert last.weekday() == 0 and date(year, 12, 1) <= last <= date(year, 12, 7), last
        # The first run's and the December recount's issue dates up to 7 Dec, and no other.
        assert issues[:len(semantics.issue_dates(sem, year))] == semantics.issue_dates(sem, year)
        late = semantics.issue_dates(bundle.values(bundle.DECEMBER, sem, obs_rules)[0], year)
        assert issues == [d for d in late if d <= date(year, 12, 7)]
        lasts.add((last.month, last.day))
    assert max(lasts) == (12, 7) and min(lasts) == (12, 1)  # 7 Dec is a Monday in 1992; 1 Dec in 2003
    assert (bundle.DECEMBER_7.name, bundle.DECEMBER_7.path) == ("december-7", "power-december-7.md")
    assert (bundle.DECEMBER_7.views, bundle.DECEMBER_7.made) == (("view-foothill-cell",), "2026-10-11")


def test_every_december_7_issue_date_has_room_and_no_window_ends_after_the_cutoff(december_7):
    chosen_sem, chosen_rules = december_7
    ends = set()
    for year in range(1991, 2026):
        for issue in semantics.issue_dates(chosen_sem, year):
            window = O.imd_dates(semantics.schedule(chosen_sem, issue))
            assert bundle.has_room(window[0], chosen_sem.rain_days, chosen_rules.sowing_cutoff), issue
            assert window[-1] <= date(year, *chosen_rules.sowing_cutoff), issue
            ends.add((window[-1].month, window[-1].day))
    # The latest window is that of Monday 7 Dec: 9 to 15 Dec, one day before the cutoff.
    assert max(ends) == (12, 15)
    assert bundle._past_cutoff(range(1991, 2026), chosen_sem, chosen_rules) == {}
    assert bundle._last_start(range(1991, 2026), chosen_sem, chosen_rules) == date(2000, 12, 9)

    for event in bundle.EVENTS:
        seasons = bundle.observed_seasons(dry_but((date(2002, 12, 5), 12.0)), YEARS, chosen_sem, chosen_rules,
                                          event, 10.0)
        assert [(len(s.slots), s.no_room) for s in seasons] == [(7, 0)] * 3
    # 2002's last window, 4 to 10 Dec, holds the rain; so every issue date of 2002 has rain before the cutoff.
    assert (seasons[1].positive, seasons[1].negative) == (1, 0)


def test_the_existing_variants_keep_their_values_and_seed_streams():
    assert bundle.FIRST_RUN == bundle.Variant(None, "power.md")
    assert (bundle.FIRST_RUN.until_stream, bundle.FIRST_RUN.simulate_stream) == (0, 1)
    december = bundle.DECEMBER
    assert (december.name, december.path, december.made) == ("december", "power-december.md", "2026-10-11")
    assert (december.season_end, december.sowing_cutoff, december.views) == ((12, 14), (12, 16), (VIEW,))
    assert (december.until_stream, december.simulate_stream) == (2, 3)
    assert (december.title, december.after, december.owner_chose, december.issued_from) == ("", (), "", "")


def test_no_row_of_the_december_7_variant_shares_a_seed_with_another_run(rules):
    pools = {(e, 0): [season(1, 1)] for e in range(2)}

    def seeds(variant, areas):
        return {tuple(job[-1]) for b in range(areas) for job in bundle.jobs(pools, b, rules, SMALL, variant).values()
                } | {tuple(bundle.until_seed(rules, variant, b, e, 0, n))
                     for b in range(areas) for e in range(2) for n in range(2)}

    by_run = [seeds(variant, areas) for variant, areas in RUNS]

    assert [len(s) for s in by_run] == [12, 24, 24]
    assert len(set().union(*by_run)) == 60
    # No seed is another with zeros added, which numpy would read as the same seed.
    assert len({seed[:2] for s in by_run for seed in s}) == 6
    streams = [stream for variant, _ in RUNS for stream in (variant.until_stream, variant.simulate_stream)]
    assert streams == [0, 1, 2, 3, 4, 5]
    # The existing runs' seeds, as they were.
    assert seeds(bundle.DECEMBER, 1) == {(20261008, 3, 0, e, 0, 0, k, x) for e in range(2) for k in range(2)
                                         for x in range(2)} | {(20261008, 2, 0, e, 0, n) for e in range(2)
                                                               for n in range(2)}


def test_december_7_table_says_whose_the_values_are_and_that_nothing_is_left_out(tmp_path):
    view_ledger(tmp_path)
    shutil.rmtree(tmp_path / "bundles/dev-2", ignore_errors=True)  # the table names this bundle and does not read it

    text = bundle.power(tmp_path, SMALL, bundle.DECEMBER_7)

    assert text == bundle.power(tmp_path, SMALL, bundle.DECEMBER_7)
    assert text.startswith("# Mandi wheat power simulation: recount for issue dates to 7 Dec\n\n"
                           "Generated by `evidence bundle power --variant december-7`; do not edit.")
    assert "- Issue dates: each Monday from 15 Oct to 7 Dec; 7-day window; sowing cutoff 16 Dec." in text
    header = " ".join(text[:text.index("## Verdict band")].split())
    assert "No real forecast enters this table." in header
    assert ("after the first run, whose table is `power.md`, and after the recount `power-december.md`. "
            "Both tables are unchanged.") in header
    assert "the last issue date is 7 Dec where the first run has 15 Nov" in header
    assert "the sowing cutoff is 16 Dec where it has 30 Nov" in header
    # Where the two values are held: not in `bundles/dev`, which the table reads; in `bundles/dev-2`, which it does not.
    assert ("Two development values are changed in code: the last issue date is 7 Dec" in header
            and "no bundle holds them" not in header)
    assert ("Every other value is the first run's, read from `bundles/dev`, which holds neither changed value. "
            "The development bundle `bundles/dev-2` holds the same two, for issuing claims; "
            "this table does not read it.") in header
    assert "No seed is shared with an earlier table or between the two areas." in header
    # Whose the values are: the owner's issue dates, as a starting value; the cutoff is still the agent's.
    assert ("The issue dates are the owner's choice, made on 2026-10-11 after reading `power-december.md`: "
            "each Monday from 15 Oct to 7 Dec.") in header
    assert "a starting value for bundle v1 and not a frozen one" in header
    assert "The cutoff of 16 Dec is still the agent's choice and open for the owner." in header
    assert "The owner has not chosen these values" not in header
    # Nothing the 14 Dec table says of issue dates left out or windows past the cutoff is claimed here.
    # 7 Mondays a season; the latest window of these three seasons starts on 4 Dec (issued 2 Dec).
    assert "No issue date is left out of that event: 0 of 21. The latest window starts on 4 Dec." in header
    assert ("The 7-day event is not cut at the cutoff. No window ends after it: 0 of 21. "
            "The latest ends on 10 Dec.") in header
    for gone in ("leaves out", "three options", "Left out", "shorter run", "windows end after it, on",
                 "Windows ending after the cutoff", "none in"):
        assert gone not in header, gone
    # The starts pooled for 4 Dec run to 10 Dec, later than 4 Dec: so "those", not "the earlier ones".
    assert ("pools fewer starts: only those that still have room. "
            "For a window starting 4 Dec it pools the starts 27 Nov to 10 Dec, 14 of 15.") in header
    assert "| `district` | 2 | 366 | 0 | 0 | 186 | 0 | 0 |" in text
    district, view = text.index("## Verdict band `district`"), text.index(f"## Exploratory view `{VIEW}`")
    assert district < view
    # Both events keep all 21 issue dates in both areas; 21 and 28 Oct of 2002 have rain before the cutoff.
    assert text.count("| 10 mm | 1991–2003 | 3 | 21 | 1 | 5% | 1 | 4 | 1 |") == 2
    assert text.count("| 10 mm | 1991–2003 | 3 | 21 | 2 | 10% | 1 | 3 | 1 |") == 2
    assert text.count("#### Hindcast pass rate by true skill") == 4


def test_the_bundle_named_for_issuing_holds_the_december_7_values(sem, obs_rules, december_7):
    variant = bundle.DECEMBER_7
    held = LEDGER_ROOT / variant.issued_from

    assert variant.issued_from == "bundles/dev-2"
    assert (semantics.load(held / "bundle.toml").season_end, O.load_rules(held / "observations.toml").sowing_cutoff
            ) == (variant.season_end, variant.sowing_cutoff) == ((12, 7), (12, 16))
    # `bundles/dev`, which the simulation reads, holds neither.
    assert (sem.season_end, obs_rules.sowing_cutoff) == ((11, 15), (11, 30))
    assert observations.DEV_BUNDLE_DIR == "bundles/dev"


def test_the_date_the_owner_chose_is_not_the_date_the_recount_was_made(sem, obs_rules):
    variant = dataclasses.replace(bundle.DECEMBER_7, made="2026-10-20")
    assert (bundle.DECEMBER_7.owner_chose, bundle.DECEMBER.owner_chose, bundle.FIRST_RUN.owner_chose
            ) == ("2026-10-11", "", "")

    note = " ".join(bundle._recount_note(variant, (sem, obs_rules), *bundle.values(variant, sem, obs_rules)))

    assert "This is a recount made on 2026-10-20, after the first run" in note
    assert "The issue dates are the owner's choice, made on 2026-10-11 after reading `power-december.md`:" in note


def test_a_recount_of_the_owners_dates_made_after_no_other_recount_names_no_table_read(sem, obs_rules):
    variant = dataclasses.replace(bundle.DECEMBER_7, after=())

    note = " ".join(bundle._recount_note(variant, (sem, obs_rules), *bundle.values(variant, sem, obs_rules)))

    assert "The issue dates are the owner's choice, made on 2026-10-11: each Monday from 15 Oct to 7 Dec." in note
    assert "after reading" not in note
    assert "whose table is `power.md` and is unchanged." in note


def test_a_recount_made_after_two_others_names_both(sem, obs_rules):
    variant = dataclasses.replace(bundle.DECEMBER_7, after=("power-december.md", "power-december-7.md"))

    note = " ".join(bundle._recount_note(variant, (sem, obs_rules), *bundle.values(variant, sem, obs_rules)))

    assert ("after the first run, whose table is `power.md`, and after the recounts `power-december.md` and "
            "`power-december-7.md`. All these tables are unchanged.") in note
    assert "Both tables" not in note


def test_climatology_pools_every_start_when_each_has_room(tmp_path):
    view_ledger(tmp_path)
    # Issue dates to 15 Nov with a cutoff of 16 Dec: a window starts on 17 Nov at the latest, and the latest
    # start its climatology then pools, 24 Nov, is before 10 Dec, the last start with room.
    variant = bundle.Variant("late-cutoff", "power-late-cutoff.md", made="2026-10-11", sowing_cutoff=(12, 16),
                             until_stream=6, simulate_stream=7)

    text = bundle.power(tmp_path, SMALL, variant)

    header = " ".join(text[:text.index("## Verdict band")].split())
    assert "- The climatology of rain before the cutoff pools every start: each has room." in header
    assert "pools fewer starts" not in header
    assert "A window that starts after 10 Dec has no room for them, and none here does." in header


def test_a_cutoff_that_leaves_no_window_room_is_an_error(sem, obs_rules):
    early = dataclasses.replace(obs_rules, sowing_cutoff=(10, 20))  # the earliest window starts on 17 Oct

    with pytest.raises(ValueError, match="no window has room for 7 dates before the sowing cutoff of 20 Oct"):
        bundle._last_start(YEARS, sem, early)
    assert bundle._last_start(YEARS, sem, obs_rules) == date(2000, 11, 13)  # the window of Monday 11 Nov 2002


@pytest.mark.parametrize("variant", [bundle.FIRST_RUN, bundle.DECEMBER, bundle.DECEMBER_7], ids=lambda v: v.path)
def test_each_table_opens_with_the_text_its_committed_file_has(tmp_path, variant):
    """The text above "## Inputs" comes from the variant and the dev bundle, not from rainfall or the simulation."""
    view_ledger(tmp_path)
    committed = (LEDGER_ROOT / variant.path).read_text(encoding="utf-8")

    text = bundle.power(tmp_path, SMALL, variant)

    assert text[:text.index("## Inputs")] == committed[:committed.index("## Inputs")]


def test_the_december_table_keeps_the_header_text_it_was_committed_with(tmp_path):
    view_ledger(tmp_path)
    committed = (LEDGER_ROOT / bundle.DECEMBER.path).read_text(encoding="utf-8")

    header = bundle.power(tmp_path, SMALL, bundle.DECEMBER)

    assert header.startswith("# Mandi wheat power simulation: December recount\n")
    for line in (
        "values are changed in code, and no bundle holds them: the last issue date is 14 Dec",
        "30 Nov. Every other value is the first run's. The exploratory views named in the",
        "No seed is shared between the two tables or between the two areas. The confidence and",
        "The owner has not chosen these values. The cutoff, the last issue date and the rule for",
        "  A window that starts after 10 Dec has no room for them. Its issue date is left out of",
        "- There were three options for such an issue date: leave it out (taken here); count it as",
        "- Near the season's end the climatology of rain before the cutoff pools fewer starts: only the",
        "  earlier ones that still have room. For a window starting 10 Dec it pools the starts",
        "  3 to 10 Dec, 8 of 15.",
    ):
        assert f"\n{line}\n" in header, line
        assert f"\n{line}\n" in committed, line

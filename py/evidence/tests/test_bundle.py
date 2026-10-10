"""U8: the power simulation. Rainfall is synthetic; the committed IMD files are not read here."""

from datetime import date, timedelta

import numpy as np
import pytest

from khetru_evidence import bundle, cli, obs_core as O, observations, score_core as S, semantics
from test_observations import DEV_BUNDLE, synthetic_ledger

SMALL = bundle.Design(
    thresholds_mm=(10.0,), counted_first_years=(2002,), simulated_first_years=(2002,), min_episodes=(1, 2),
    confidences=(0.9,), margins=(0.1,), skills=(0.2, 0.5), replicates=20, bootstrap_resamples=200,
    season_cap=20,
)
YEARS = (1991, 2002, 2003)


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

    for skill in (0.1, 0.4):
        forecasts = bundle.draw_forecasts(rng, slots, skill)
        assert all(0 < p < 1 for p in forecasts)
        assert S.bss(bundle.rows(slots, forecasts)) == pytest.approx(skill, abs=0.02)
        # Reliable: the event held about as often as forecasts near 0.5 said.
        near = [h for h, p in zip(held, forecasts) if 0.4 < p < 0.6]
        assert not near or np.mean(near) == pytest.approx(0.5, abs=0.1)


def test_a_simulated_skill_must_be_inside_0_and_1():
    for skill in (0.0, 1.0):
        with pytest.raises(ValueError, match="inside 0-1"):
            bundle.draw_forecasts(np.random.default_rng(1), season(1, 1).slots, skill)


def test_simulation_repeats_for_a_seed_and_tallies_every_record(rules):
    pool = [season(1, 1, year) for year in range(2000, 2020)]

    def run(seed, skill=0.5):
        return bundle.simulate(pool, pool, skill, rules, SMALL, np.random.default_rng(seed))

    first = run(7)
    assert first == run(7)
    assert run(7, skill=0.1) != run(8, skill=0.1)
    assert first.replicates == 20
    assert all(sum(tally.values()) == 20 for tally in (*first.live_equal.values(), *first.live_none.values()))
    # A strong forecast over 20 seasons passes; a live season that states climatology never does.
    assert first.passes[0.9] == 20
    assert first.live_none[0.9, 0.1][S.PASS] == 0
    assert run(7, skill=0.05).passes[0.9] < 20


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

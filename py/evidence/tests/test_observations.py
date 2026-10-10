"""U4: IMD observation records, band window totals, rain episodes, climatology, base rates.

Grids are synthetic. The band map is one district-wide band over two cells,
31.50 N at 77.00 E (weight 0.75) and 77.25 E (weight 0.25).
"""

import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from khetru_evidence import bands as B
from khetru_evidence import cli, fetch_observations, obs_core as O, observations, semantics

REPO = Path(__file__).resolve().parents[3]
LEDGER = REPO / "ledger/mandi-wheat"
DEV_BUNDLE = LEDGER / "bundles/dev"

BAND_MAP = B.BandMap(
    cells=(B.Cell(31.5, 77.0, 300.0, 0.75, B.DISTRICT), B.Cell(31.5, 77.25, 100.0, 0.25, B.DISTRICT)),
    reason=B.NO_INDEPENDENT_GAUGES,
)
SHA = "ab" * 32


@pytest.fixture
def sem():
    return semantics.load(DEV_BUNDLE / "bundle.toml")


@pytest.fixture
def rules():
    return O.load_rules(DEV_BUNDLE / "observations.toml")


def india(days: int, fill: float = 0.0) -> np.ndarray:
    return np.full((days, O.GRD_NLAT, O.GRD_NLON), fill)


def at(lat: float, lon: float) -> tuple[int, int]:
    return round((lat - O.GRD_LAT0) / 0.25), round((lon - O.GRD_LON0) / 0.25)


def record(grid, first=date(2026, 10, 1), product="final", retrieved="20261010") -> dict:
    return O.build_record(grid, product=product, vintage=f"{product}-r{retrieved}", first=first,
                          source_url="https://example.test/rain", raw=[("x.grd", SHA)])


def band_of(values, first=date(2026, 10, 21)) -> dict[date, float]:
    """A band series with ``values`` on consecutive dates from ``first``."""
    return {first + timedelta(days=i): float(v) for i, v in enumerate(values)}


def season(year: int, wet: dict[str, float]) -> dict[date, float]:
    """Every date of September to December of ``year``, dry but for ``wet`` (MM-DD -> mm)."""
    band = band_of([0.0] * 122, date(year, 9, 1))
    for month_day, mm in wet.items():
        band[date.fromisoformat(f"{year}-{month_day}")] = mm
    return band


# --- the record --------------------------------------------------------------


def test_grd_bytes_decode_south_to_north_with_missing_as_nan():
    raw = india(2).astype("<f4")
    raw[0, 0, 1] = 3.5  # second value of the file: 6.5 N, 66.75 E
    raw[1, at(31.5, 77.0)[0], at(31.5, 77.0)[1]] = O.GRD_MISSING

    grid = O.decode_grd(raw.tobytes(), 2)

    assert grid.shape == (2, 129, 135)
    assert grid[0, 0, 1] == 3.5
    assert np.isnan(grid[1][at(31.5, 77.0)])
    with pytest.raises(O.ObservationError, match="expected"):
        O.decode_grd(raw.tobytes(), 3)


def test_record_holds_the_mandi_box_cells_of_the_forecast_records():
    from khetru_evidence import forecasts

    grid = india(2)
    grid[1][at(31.5, 77.0)] = 4.25
    grid[0][at(31.5, 77.25)] = np.nan

    rec = record(grid)

    lats, lons = forecasts.box_cells(forecasts.MANDI_BOX)
    assert (rec["lats"], rec["lons"]) == (lats, lons)
    assert rec["values"][1][lats.index(31.5)][lons.index(77.0)] == 4.25
    assert rec["values"][0][lats.index(31.5)][lons.index(77.25)] is None
    assert O.record_dates(rec) == (date(2026, 10, 1), date(2026, 10, 2))
    assert O.record_path(rec) == "observations/imd-final-r20261010-20261001-20261002.json.gz"


def test_record_file_round_trips_and_is_stable():
    rec = record(india(3, 1.5))
    data = O.encode_record(rec)

    assert O.decode_record(data) == rec
    assert O.encode_record(O.decode_record(data)) == data


@pytest.mark.parametrize("damage", [
    {"vintage": "final-latest"},
    {"vintage": "realtime-r20261010"},  # not the record's product
    {"lats": [31.4, 31.65]},
    {"raw": []},
    {"values": [[[-1.0]]]},
    {"units": "cm"},
])
def test_damaged_record_is_refused(damage):
    with pytest.raises(ValueError):
        O.validate_record({**record(india(1)), **damage})


def test_saving_is_write_once(tmp_path):
    rec = record(india(1))

    assert O.write_record(tmp_path, rec) == (O.record_path(rec), True)
    assert O.write_record(tmp_path, rec) == (O.record_path(rec), False)
    with pytest.raises(O.ObservationError, match="write-once"):
        O.write_record(tmp_path, record(india(1, 2.0)))


def test_provisional_and_final_copies_of_a_day_are_both_kept_and_loaded_by_vintage(tmp_path):
    day = date(2026, 10, 21)
    O.write_record(tmp_path, record(india(1, 3.0), first=day, product="realtime", retrieved="20261022"))
    O.write_record(tmp_path, record(india(1, 5.0), first=day, product="final", retrieved="20270601"))

    assert observations.vintages(tmp_path) == ["final-r20270601", "realtime-r20261022"]
    provisional = observations.load(tmp_path, "realtime-r20261022")
    final = observations.load(tmp_path, "final-r20270601")
    assert provisional.dates == final.dates == (day,)
    assert O.band_daily(provisional, BAND_MAP, B.DISTRICT, 1.0)[day] == 3.0
    assert O.band_daily(final, BAND_MAP, B.DISTRICT, 1.0)[day] == 5.0


def test_records_of_one_series_must_not_overlap_or_mix_vintages():
    a = record(india(2), first=date(2026, 10, 1))
    with pytest.raises(O.ObservationError, match="overlap"):
        O.daily([a, record(india(2), first=date(2026, 10, 2))])
    with pytest.raises(O.ObservationError, match="share a vintage"):
        O.daily([a, record(india(1), first=date(2026, 10, 3), retrieved="20261011")])


# --- band values, windows and outcomes ---------------------------------------


def test_band_value_is_the_area_weighted_mean_of_the_cells():
    grid = india(1)
    grid[0][at(31.5, 77.0)] = 8.0
    grid[0][at(31.5, 77.25)] = 4.0

    band = O.band_daily(O.daily([record(grid)]), BAND_MAP, B.DISTRICT, 1.0)

    assert band[date(2026, 10, 1)] == pytest.approx(0.75 * 8.0 + 0.25 * 4.0)


def test_window_total_holds_at_10_mm_and_not_at_12():
    band = band_of([0, 0, 4, 6, 0, 0, 1])

    total = O.window_total(band, list(band))

    assert total == 11.0
    assert O.outcome(total, 10.0) == O.HELD
    assert O.outcome(total, 12.0) == O.NOT_HELD


def test_window_starts_at_the_first_03_utc_rain_day_after_issue_not_on_the_issue_day(sem):
    issue = date(2026, 10, 19)  # a Monday
    schedule = semantics.schedule(sem, issue)

    dates = O.imd_dates(schedule)

    # The first rain-day runs Tuesday 03 UTC to Wednesday 03 UTC: IMD's Wednesday.
    assert schedule.rain_days[0][1] == datetime(2026, 10, 21, 3, tzinfo=UTC)
    assert dates == O.run_dates(date(2026, 10, 21), 7)
    assert issue not in dates and date(2026, 10, 20) not in dates
    # Rain recorded against the issue day or the day after falls outside the window.
    band = band_of([50.0, 50.0, *[0.0] * 7], first=issue)
    assert O.window_total(band, dates) == 0.0


def test_rain_days_that_do_not_end_at_0830_ist_are_refused(sem):
    from dataclasses import replace

    off = semantics.schedule(replace(sem, rain_day_start_hour_utc=0), date(2026, 10, 19))
    with pytest.raises(O.ObservationError, match="03:00 UTC"):
        O.imd_dates(off)


def test_missing_cell_day_below_the_coverage_share_is_unverifiable():
    grid = india(7, 2.0)
    grid[3][at(31.5, 77.25)] = np.nan  # a quarter of the band on one day
    series = O.daily([record(grid, first=date(2026, 10, 21))])
    dates = series.dates

    strict = O.band_daily(series, BAND_MAP, B.DISTRICT, 1.0)
    loose = O.band_daily(series, BAND_MAP, B.DISTRICT, 0.7)

    assert O.outcome(O.window_total(strict, dates), 10.0) == O.UNVERIFIABLE
    assert O.outcome(O.window_total(loose, dates), 10.0) == O.HELD
    # A date with no record at all is as unverifiable as one with a missing cell.
    assert O.window_total(loose, [*dates, dates[-1] + timedelta(days=1)]) is None


def test_rain_before_the_cutoff_looks_at_every_run_of_days():
    band = band_of([0, 0, 0, 0, 0, 0, 0, 0, 6, 5, 0, 0])
    first, last = min(band), max(band)

    assert O.rain_before(band, first, last, 7, 10.0) == O.HELD
    assert O.rain_before(band, first, last, 7, 12.0) == O.NOT_HELD
    assert O.rain_before(band, first, first + timedelta(days=8), 7, 10.0) == O.NOT_HELD
    assert O.rain_before(band, first, last + timedelta(days=1), 7, 12.0) == O.UNVERIFIABLE


# --- episodes ----------------------------------------------------------------


def test_rain_days_fewer_than_the_gap_apart_are_one_episode():
    first = date(2026, 10, 21)
    close = band_of([5, 0, 0, 5, 0, 0, 0])  # two dry days between
    apart = band_of([5, 0, 0, 0, 5, 0, 0])  # three dry days between

    assert O.rain_episodes(close, first, first + timedelta(days=6), 1.0, 3) == [
        (first, first + timedelta(days=3))
    ]
    assert len(O.rain_episodes(apart, first, first + timedelta(days=6), 1.0, 3)) == 2
    assert O.rain_episodes(band_of([0.9] * 7), first, first + timedelta(days=6), 1.0, 3) == []


def test_one_rain_episode_across_two_held_windows_is_one_positive_episode():
    # Windows of 7 dates; rain on the last day of the first and the first day of the second.
    straddling = band_of([0, 0, 0, 0, 0, 0, 12, 12, 0, 0, 0, 0, 0, 0])
    separate = band_of([12, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 12])
    dates = sorted(straddling)
    windows = [dates[:7], dates[7:]]

    assert O.episode_counts(straddling, windows, 10.0, 1.0, 3) == (1, 0)
    assert O.episode_counts(separate, windows, 10.0, 1.0, 3) == (2, 0)


def test_dry_windows_in_a_row_are_one_negative_episode():
    values = [0] * 14 + [12] + [0] * 6 + [0] * 14  # dry, dry, held, dry, dry
    band = band_of(values)
    dates = sorted(band)
    windows = [dates[i:i + 7] for i in range(0, 35, 7)]

    assert O.episode_counts(band, windows, 10.0, 1.0, 3) == (1, 2)
    assert O.episode_counts(band, windows[:2], 10.0, 1.0, 3) == (0, 1)


def test_unverifiable_windows_count_for_no_episode():
    band = band_of([0] * 7 + [float("nan")] * 7 + [0] * 7)
    dates = sorted(band)
    windows = [dates[i:i + 7] for i in range(0, 21, 7)]

    assert O.episode_counts(band, windows, 10.0, 1.0, 3) == (0, 1)
    assert O.episode_counts(band, windows[1:2], 10.0, 1.0, 3) == (0, 0)


# --- climatology -------------------------------------------------------------


def climatology(band, first, years, half_window_days=0):
    return O.climatology(band, first, years=years, days=7, threshold_mm=10.0,
                         half_window_days=half_window_days, pseudo_count=0.5)


def test_leave_one_season_out_climatology_excludes_the_season():
    years = range(2001, 2011)
    band = {}
    for year in years:
        # 2005 has rain in every week; every other year is dry.
        wet = {f"{m:02d}-{d:02d}": 3.0 for m in (10, 11) for d in range(1, 31)} if year == 2005 else {}
        band |= season(year, wet)
    first = date(2005, 10, 21)

    without = climatology(band, first, [y for y in years if y != 2005])
    with_it = climatology(band, first, years)

    assert (without.events, without.windows) == (0, 9)
    assert (with_it.events, with_it.windows) == (1, 10)
    assert without.probability < with_it.probability


def test_climatology_probability_is_never_0_or_1():
    years = range(2001, 2006)
    dry = {d: v for year in years for d, v in season(year, {}).items()}
    wet = {d: 5.0 for d in dry}

    never = climatology(dry, date(2003, 10, 21), years)
    always = climatology(wet, date(2003, 10, 21), years)

    assert never.probability == pytest.approx(0.5 / 6) and never.probability > 0
    assert always.probability == pytest.approx(5.5 / 6) and always.probability < 1
    with pytest.raises(O.ObservationError, match="pseudo_count"):
        O.climatology(dry, date(2003, 10, 21), years=years, days=7, threshold_mm=10.0,
                      half_window_days=0, pseudo_count=0.0)


def test_climatology_pools_windows_around_the_date_and_skips_unverifiable_ones():
    band = season(2001, {"10-25": 12.0}) | season(2002, {})
    del band[date(2002, 10, 24)]
    first = date(2003, 10, 21)

    exact = climatology(band, first, [2001, 2002])
    pooled = climatology(band, first, [2001, 2002], half_window_days=2)

    # 2001: the window from 21 Oct holds. 2002: it has a date with no value.
    assert (exact.events, exact.windows) == (1, 1)
    # 2001: windows from 19 to 23 Oct all hold 25 Oct. 2002: none is complete.
    assert (pooled.events, pooled.windows) == (5, 5)


# --- rules, fetching and the base-rate table ---------------------------------


def test_dev_observation_rules_load(rules):
    assert rules.coverage_share == 1.0
    assert (rules.wet_day_mm, rules.dry_day_gap_days) == (1.0, 3)
    assert rules.climatology_first_year == 1991
    assert rules.sowing_cutoff == (11, 30)


def test_final_and_realtime_downloads_become_records():
    year = india(365, 1.0).astype("<f4").tobytes()
    days = {date(2026, 10, d): india(1, float(d)).astype("<f4").tobytes() for d in (5, 6, 7)}

    final = fetch_observations.final_record(year, 2025, "final-r20261010")
    realtime = fetch_observations.realtime_record(days, "realtime-r20261010")

    assert O.record_dates(final)[::364] == (date(2025, 1, 1), date(2025, 12, 31))
    assert final["raw"] == [{"name": "2025.grd", "sha256": hashlib.sha256(year).hexdigest()}]
    assert O.record_dates(realtime) == tuple(days)
    assert [r["name"] for r in realtime["raw"]] == ["2026-10-05.grd", "2026-10-06.grd", "2026-10-07.grd"]
    assert realtime["values"][2][0][0] == 7.0
    with pytest.raises(O.ObservationError, match="consecutive"):
        fetch_observations.realtime_record({d: days[d] for d in list(days)[::2]}, "realtime-r20261010")


def synthetic_ledger(root: Path) -> None:
    """Three seasons of final data, dry but for 12 mm on 1 Nov 2002, with the dev bundle and a band map."""
    (root / "bundles/dev").mkdir(parents=True)
    for name in ("bundle.toml", "observations.toml"):
        (root / "bundles/dev" / name).write_bytes((DEV_BUNDLE / name).read_bytes())
    (root / "bands").mkdir()
    (root / B.BAND_MAP_PATH).write_bytes(B.encode(BAND_MAP))
    for year in (1991, 2002, 2003):
        grid = india(365)
        if year == 2002:
            grid[date(2002, 11, 1).timetuple().tm_yday - 1] = 12.0
        O.write_record(root, record(grid, first=date(year, 1, 1)))


def test_base_rate_table_counts_windows_and_episodes(tmp_path):
    synthetic_ledger(tmp_path)

    text = observations.base_rates(tmp_path, "final-r20261010")

    # 4 Mondays in each season. In 2002 they are 21 and 28 Oct, 4 and 11 Nov, and
    # the second window (30 Oct to 5 Nov) holds 1 Nov, with a dry one before it.
    assert "- Seasons: 1991 to 2003 (3)." in text
    assert "| 10 mm | 12 | 1 | 8.3% | 1 | 4 | 1 of 3 | 0 |" in text
    assert "| 20 mm | 12 | 0 | 0.0% | 0 | 3 | 0 of 3 | 0 |" in text
    # Issue dates of 2002 whose window starts by 1 Nov: 21 and 28 Oct.
    assert "| 10 mm | 12 | 2 | 16.7% | 1 of 3 |" in text
    assert "| 2002 | 1/4 (1+, 2−) | 1/4 (1+, 2−) | 1/4 (1+, 2−) | 0/4 (0+, 1−) |" in text
    assert "| `final-r20261010` | 183 | 366 | 0 | 0 |" in text
    assert observations.base_rates(tmp_path, "final-r20261010") == text


def test_base_rates_check_fails_when_the_table_was_edited(tmp_path, capsys):
    root = tmp_path / "ledger" / observations.LEDGER
    synthetic_ledger(root)
    args = ["observe", "base-rates", "--vintage", "final-r20261010", "--repo", str(tmp_path)]

    assert cli.main(args) == 0
    assert cli.main([*args, "--check"]) == 0
    table = root / observations.BASE_RATES_PATH
    table.write_text(table.read_text(encoding="utf-8").replace("8.3%", "88.3%"), encoding="utf-8")
    assert cli.main([*args, "--check"]) == 1
    assert "differs" in capsys.readouterr().err


# --- the committed observations ----------------------------------------------


def committed_vintages() -> tuple[str, str | None]:
    text = (LEDGER / observations.BASE_RATES_PATH).read_text(encoding="utf-8")
    final = re.search(r"vintage `(final-r\d{8})`", text)[1]
    provisional = re.search(r"both `(realtime-r\d{8})`", text)
    return final, provisional and provisional[1]


def test_committed_observation_files_are_canonical_records_under_their_own_names():
    files = sorted((LEDGER / O.OBSERVATIONS_DIR).glob("*"))
    assert files
    for path in files:
        rec = O.decode_record(path.read_bytes())
        assert O.record_path(rec) == f"{O.OBSERVATIONS_DIR}/{path.name}"


def test_committed_base_rate_table_reproduces_from_the_committed_files():
    final, provisional = committed_vintages()

    assert observations.base_rates(LEDGER, final, provisional) == (
        LEDGER / observations.BASE_RATES_PATH
    ).read_text(encoding="utf-8")

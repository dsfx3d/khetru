from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from khetru_evidence import semantics

REPO = Path(__file__).resolve().parents[3]
DEV_BUNDLE = REPO / "ledger/mandi-wheat/bundles/dev/bundle.toml"


@pytest.fixture
def sem():
    return semantics.load(DEV_BUNDLE)


def at(*args):
    return datetime(*args, tzinfo=UTC)


def test_monday_issue_schedule(sem):
    issue = date(2026, 10, 19)  # a Monday
    issued_at = at(2026, 10, 19, 10, 30)
    s = semantics.schedule(sem, issue)

    assert s.run_init == at(2026, 10, 19, 0)
    assert s.run_available == at(2026, 10, 19, 9)
    assert (s.step_start_hours, s.step_end_hours) == (24, 192)
    assert s.forecast_start == at(2026, 10, 20, 0)
    assert s.forecast_end == at(2026, 10, 27, 0)
    # First 03 UTC after a 10:30 UTC Monday issue is Tuesday 03 UTC.
    assert s.window_start == at(2026, 10, 20, 3)
    assert s.run_available <= issued_at < s.window_start
    assert s.window_end == at(2026, 10, 27, 3)
    assert s.expiry == s.window_end
    assert s.late_cutoff == s.window_start
    assert len(s.rain_days) == 7
    assert s.rain_days[0] == (at(2026, 10, 20, 3), at(2026, 10, 21, 3))
    assert s.rain_days[-1] == (at(2026, 10, 26, 3), at(2026, 10, 27, 3))
    assert all(end - start == timedelta(days=1) for start, end in s.rain_days)


def test_issue_dates_are_mondays_in_season_inclusive(sem):
    assert semantics.issue_dates(sem, 2026) == [
        date(2026, 10, 19),
        date(2026, 10, 26),
        date(2026, 11, 2),
        date(2026, 11, 9),
    ]
    # 2027: 18 Oct is the first Monday and 15 Nov itself is a Monday.
    dates = semantics.issue_dates(sem, 2027)
    assert dates[0] == date(2027, 10, 18)
    assert dates[-1] == date(2027, 11, 15)


@pytest.mark.parametrize("bad", [date(2026, 10, 20), date(2026, 10, 12), date(2026, 11, 16)])
def test_schedule_refuses_non_issue_dates(sem, bad):
    with pytest.raises(ValueError, match="not an issue date"):
        semantics.schedule(sem, bad)


def test_values_come_from_the_bundle(tmp_path):
    text = DEV_BUNDLE.read_text().replace("forecast_step_end_hours = 192", "forecast_step_end_hours = 168")
    custom = tmp_path / "bundle.toml"
    custom.write_text(text)
    s = semantics.schedule(semantics.load(custom), date(2026, 10, 19))
    assert s.step_end_hours == 168
    assert s.forecast_end == at(2026, 10, 26, 0)

"""Time rules for issue dates, forecast runs and observed windows (KTD3).

Pure functions over values read from a bundle's ``[semantics]`` table. Forecast,
observation, claim and scoring code all take their times from here.
"""

import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class Semantics:
    issue_weekday: int  # 0 = Monday
    season_start: tuple[int, int]  # (month, day)
    season_end: tuple[int, int]
    run_hour_utc: int
    run_available_after_hours: int
    forecast_step_start_hours: int
    forecast_step_end_hours: int
    rain_day_start_hour_utc: int
    rain_days: int


@dataclass(frozen=True)
class Schedule:
    issue_date: date
    run_init: datetime
    run_available: datetime
    step_start_hours: int
    step_end_hours: int
    forecast_start: datetime
    forecast_end: datetime
    rain_days: tuple[tuple[datetime, datetime], ...]  # [start, end) per IMD rain-day
    window_start: datetime
    window_end: datetime

    @property
    def expiry(self) -> datetime:
        return self.window_end

    @property
    def late_cutoff(self) -> datetime:
        return self.window_start


def load(bundle_toml: Path) -> Semantics:
    with open(bundle_toml, "rb") as f:
        s = tomllib.load(f)["semantics"]
    return Semantics(
        issue_weekday=WEEKDAYS.index(s["issue_weekday"]),
        season_start=_month_day(s["season_start"]),
        season_end=_month_day(s["season_end"]),
        run_hour_utc=int(s["run_hour_utc"]),
        run_available_after_hours=int(s["run_available_after_hours"]),
        forecast_step_start_hours=int(s["forecast_step_start_hours"]),
        forecast_step_end_hours=int(s["forecast_step_end_hours"]),
        rain_day_start_hour_utc=int(s["rain_day_start_hour_utc"]),
        rain_days=int(s["rain_days"]),
    )


def issue_dates(sem: Semantics, year: int) -> list[date]:
    """Every issue date of the season in ``year``, inclusive of both ends."""
    day = date(year, *sem.season_start)
    end = date(year, *sem.season_end)
    day += timedelta(days=(sem.issue_weekday - day.weekday()) % 7)
    dates = []
    while day <= end:
        dates.append(day)
        day += timedelta(days=7)
    return dates


def is_issue_date(sem: Semantics, day: date) -> bool:
    return day in issue_dates(sem, day.year)


def schedule(sem: Semantics, issue_date: date) -> Schedule:
    if not is_issue_date(sem, issue_date):
        raise ValueError(f"{issue_date} is not an issue date")
    run_init = datetime(
        issue_date.year, issue_date.month, issue_date.day, sem.run_hour_utc, tzinfo=UTC
    )
    run_available = run_init + timedelta(hours=sem.run_available_after_hours)
    # A claim can only be issued once its run is available, so the first rain-day
    # boundary after availability is the first one after any valid issue time.
    window_start = _next_hour(run_available, sem.rain_day_start_hour_utc)
    day = timedelta(days=1)
    rain_days = tuple(
        (window_start + i * day, window_start + (i + 1) * day) for i in range(sem.rain_days)
    )
    return Schedule(
        issue_date=issue_date,
        run_init=run_init,
        run_available=run_available,
        step_start_hours=sem.forecast_step_start_hours,
        step_end_hours=sem.forecast_step_end_hours,
        forecast_start=run_init + timedelta(hours=sem.forecast_step_start_hours),
        forecast_end=run_init + timedelta(hours=sem.forecast_step_end_hours),
        rain_days=rain_days,
        window_start=window_start,
        window_end=rain_days[-1][1],
    )


def _next_hour(after: datetime, hour: int) -> datetime:
    """First time strictly after ``after`` whose UTC clock reads ``hour``:00."""
    candidate = after.replace(hour=hour, minute=0, second=0, microsecond=0)
    while candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _month_day(text: str) -> tuple[int, int]:
    month, day = text.split("-")
    return int(month), int(day)

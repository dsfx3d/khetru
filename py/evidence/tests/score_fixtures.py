"""Synthetic inputs shared by the scoring tests.

On top of ``claim_fixtures``: IMD records that put the same rain on both cells
of the band map, so the band value of a day is the number asked for. The final
vintage the dev bundle names holds October and November of 2023 to 2025: 2023
has 20 mm every day and the other two are dry, so the climatology of any window
is 15 held windows out of 45, (15 + 0.5) / (45 + 1).
"""

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from claim_fixtures import CODE, MONDAY, TOTALS_30_OF_51, make_ledger, record
from khetru_evidence import claims, forecasts as fc, obs_core as O
from khetru_evidence import ledger as L

SHA = "ab" * 32
FINAL = "final-r20261010"
REALTIME = "realtime-r20261028"
CLIMATOLOGY = round(15.5 / 46, 6)
# The 19 Oct claim's window is IMD's 21 to 27 Oct; it has closed by the 28th.
AFTER_WINDOW = datetime(2026, 10, 28, 6, tzinfo=UTC)
WINDOW_FIRST = date(2026, 10, 21)
ROW = round((31.5 - O.GRD_LAT0) / 0.25)
COLS = [round((lon - O.GRD_LON0) / 0.25) for lon in (77.0, 77.25)]


def save_rain(root: Path, first: date, daily, vintage: str) -> None:
    """Save one record of ``vintage`` with ``daily`` mm (None: no value) on consecutive dates from ``first``."""
    grid = np.zeros((len(daily), O.GRD_NLAT, O.GRD_NLON))
    grid[:, ROW, COLS] = np.array([np.nan if v is None else v for v in daily], dtype=float)[:, None]
    O.write_record(root, O.build_record(
        grid, product=vintage.split("-")[0], vintage=vintage, first=first,
        source_url="https://example.test/rain", raw=[("x.grd", SHA)]))


def week(total: float | None) -> list:
    """Seven days from 21 Oct 2026 whose rain adds up to ``total``; None leaves a day with no value."""
    return [None] + [0.0] * 6 if total is None else [total] + [0.0] * 6


def scoring_ledger(root: Path, now: datetime = AFTER_WINDOW) -> L.Ledger:
    """A ledger with the dev bundle, the synthetic band map and the final vintage for climatology."""
    lg = make_ledger(root, now)
    for year, mm in ((2023, 20.0), (2024, 0.0), (2025, 0.0)):
        save_rain(lg.root, date(year, 10, 1), [mm] * 61, FINAL)
    return lg


def issue(lg: L.Ledger, day: date = MONDAY, kind: str = "exploratory", bundle: str = "dev", **options) -> dict:
    """Issue ``day``'s claim (30 of 51 members, p = 30.5 / 52) and return its entry."""
    if not options.get("abstain_if_missing"):
        fc.write_record(lg.root, record(TOTALS_30_OF_51, init=datetime(day.year, day.month, day.day, tzinfo=UTC)))
    at_issue = L.Ledger(lg.root, clock=lambda: datetime(day.year, day.month, day.day, 10, 30, tzinfo=UTC))
    [entry] = claims.issue(at_issue, day, bundle=bundle, kind=kind, code=CODE, **options)
    return entry

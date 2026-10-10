"""Synthetic inputs shared by the claim tests.

The band map is one district-wide band over two cells, 31.50 N at 77.00 E
(weight 0.75) and 77.25 E (weight 0.25). A record gives every member the same
rain on both cells, so a member's band-mean window total is the number asked for.
"""

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

from khetru_evidence import bands as B
from khetru_evidence import forecasts as fc
from khetru_evidence import ledger as L

REPO = Path(__file__).resolve().parents[3]
REAL_LEDGER = REPO / "ledger/mandi-wheat"
DEV_BUNDLE = REAL_LEDGER / "bundles/dev"

BAND_MAP = B.BandMap(
    cells=(B.Cell(31.5, 77.0, 300.0, 0.75, B.DISTRICT), B.Cell(31.5, 77.25, 100.0, 0.25, B.DISTRICT)),
    reason=B.NO_INDEPENDENT_GAUGES,
)
MONDAY = date(2026, 10, 19)
CODE = "c0de" * 10
# 30 of 51 members at or above 10 mm, one of them exactly on it.
TOTALS_30_OF_51 = [10.0] + [12.5] * 29 + [9.999] + [0.0] * 20


def record(totals, init=datetime(2026, 10, 19, tzinfo=UTC), lons=(77.0, 77.25)) -> dict:
    """A record whose members' 24-192 h totals are ``totals`` (control first) on every cell."""
    def series(total):
        return [[[x] * len(lons)] for x in (0.0, 1.0, round(1.0 + total, 3))]

    return {
        "format": fc.FORMAT, "source": "opendata", "init": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_cycle": "ecmwf-gpi-158", "source_url": "https://example.test/ens/", "raw_sha256": "ab" * 32,
        "units": "mm", "lats": [31.5], "lons": list(lons), "steps": [0, 24, 192],
        "control": series(totals[0]),
        "perturbed": {str(i): series(t) for i, t in enumerate(totals[1:], start=1)},
    }


def make_ledger(root: Path, now: datetime) -> L.Ledger:
    """A ledger at ``root`` with the dev bundle and the synthetic band map, its clock fixed at ``now``."""
    shutil.copytree(DEV_BUNDLE, root / "bundles/dev")
    (root / "bands").mkdir(parents=True)
    (root / B.BAND_MAP_PATH).write_bytes(B.encode(BAND_MAP))
    return L.Ledger(root, clock=lambda: now)

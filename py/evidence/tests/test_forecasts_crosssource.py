"""Network tests for the TIGGE adapter and the KTD2 cross-source check.

Skipped by default: they need ECMWF Data Store credentials in ``~/.cdsapirc``
(``url: https://ecds.ecmwf.int/api`` plus a personal token) and real downloads,
and the cross-source test also needs a saved rabi 2026 open-data run, which the
daily archive only starts producing in Oct 2026 (TIGGE adds a 48 h delay).
Run them with ``EVIDENCE_NETWORK_TESTS=1``.

No date here touches a pre-2026 Oct–Nov forecast (KTD11); the guard would refuse
it anyway.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from khetru_evidence import fetch_forecasts as ff
from khetru_evidence import forecasts as fc

REPO = Path(__file__).resolve().parents[3]
INPUTS = REPO / "ledger" / "mandi-wheat" / "inputs"
CACHE = REPO / ".cache" / "evidence"


def _has_credentials() -> bool:
    return Path("~/.cdsapirc").expanduser().exists() or bool(
        os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY")
    )


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("EVIDENCE_NETWORK_TESTS") != "1",
        reason="network test: set EVIDENCE_NETWORK_TESTS=1 to download from ECMWF",
    ),
    pytest.mark.skipif(
        not _has_credentials(),
        reason="no ECMWF Data Store credentials (~/.cdsapirc or CDSAPI_URL/CDSAPI_KEY)",
    ),
]

# One non-Oct–Nov init per ECMWF ENS resolution era in TIGGE (from Oct 2006).
# Mid-month inits keep every step (to 360 h) outside Oct and Nov.
ERA_INITS = {
    "TL399 VAREPS (2006-2009)": datetime(2007, 1, 15, tzinfo=UTC),
    "TL639 (2010-2016)": datetime(2010, 7, 15, tzinfo=UTC),
    "TCo639 (2016-2023)": datetime(2016, 7, 15, tzinfo=UTC),
    "TCo1279, 48r1 (2023-2024)": datetime(2023, 7, 15, tzinfo=UTC),
    "49r1 (2024-2025)": datetime(2025, 7, 15, tzinfo=UTC),
}

# Window and tolerance for the cross-source check: the KTD3 forecast window, and
# the ensemble-mean Mandi-box total agreeing within 5 % or 0.5 mm, whichever is
# larger. Both sources carry the same model run, so differences come only from
# interpolation and GRIB packing.
WINDOW = (24, 192)
RELATIVE_TOLERANCE = 0.05
ABSOLUTE_TOLERANCE_MM = 0.5


@pytest.mark.parametrize("era", list(ERA_INITS))
def test_tigge_decodes_one_date_per_ens_era(era):
    init = ERA_INITS[era]
    assert not ff.is_protected(init)
    record = ff.fetch_tigge(init, repo=REPO, cache_dir=CACHE)
    assert record["steps"] == list(fc.TIGGE_STEPS)
    assert fc.member_count(record) == 51
    assert (record["lats"], record["lons"]) == fc.box_cells(fc.MANDI_BOX)
    assert np.isfinite(fc.window_totals(record, *WINDOW)).all()


def _saved_rabi_2026_run() -> dict | None:
    wanted = os.environ.get("EVIDENCE_CROSSSOURCE_INIT")  # e.g. 2026101900
    for path in sorted(INPUTS.glob("ens-opendata-2026*.json.gz")):
        if wanted is None or path.name == f"ens-opendata-{wanted}.json.gz":
            return fc.decode_record(path.read_bytes())
    return None


def _box_mean(record: dict) -> float:
    """Ensemble-mean window total over the Mandi box cells without margin.

    Stands in for the band mean until U3's band map exists.
    """
    lats, lons = fc.box_cells(fc.MANDI_BOX, margin=0)
    rows = [record["lats"].index(x) for x in lats]
    cols = [record["lons"].index(x) for x in lons]
    totals = fc.window_totals(record, *WINDOW)[:, rows][:, :, cols]
    return float(totals.mean())


def test_tigge_and_saved_opendata_agree_on_a_rabi_2026_init():
    saved = _saved_rabi_2026_run()
    if saved is None:
        pytest.skip("no saved rabi 2026 open-data run under ledger/mandi-wheat/inputs yet")
    tigge = ff.fetch_tigge(fc.record_init(saved), repo=REPO, cache_dir=CACHE)

    assert fc.member_count(tigge) == fc.member_count(saved)
    a, b = _box_mean(saved), _box_mean(tigge)
    assert abs(a - b) <= max(ABSOLUTE_TOLERANCE_MM, RELATIVE_TOLERANCE * max(a, b)), (a, b)

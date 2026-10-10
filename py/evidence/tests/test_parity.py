"""U6 parity: one claim body, whichever driver records it.

The live/exploratory driver and the hindcast driver (U9) both turn a
``claim_core`` body into a ledger entry through ``claims.entry``; the entries
may differ only in ``kind``, ``run_id`` and the ``id`` built from them.
"""

from datetime import UTC, datetime

import pytest

from claim_fixtures import CODE, MONDAY, TOTALS_30_OF_51, make_ledger, record
from khetru_evidence import claims, forecasts as fc

DRIVER_ONLY = ("kind", "run_id", "id")


@pytest.mark.parametrize("abstain", [False, True])
def test_live_and_hindcast_entries_differ_only_in_kind_and_run(tmp_path, abstain):
    lg = make_ledger(tmp_path / "mandi-wheat", datetime(2026, 10, 19, 10, 30, tzinfo=UTC))
    saved = None
    if not abstain:
        rel, _ = fc.write_record(lg.root, record(TOTALS_30_OF_51))
        saved = lg.root / rel
    [body] = claims.bodies(lg.root, claims.Bundle(lg.root, "dev"), MONDAY, saved)
    assert not set(DRIVER_ONLY) & set(body)

    live = claims.entry(body, ledger=lg.name, kind="exploratory", bundle="dev", code=CODE)
    hindcast = claims.entry(body, ledger=lg.name, kind="hindcast", bundle="dev", code=CODE, run_id="dev-run-1")

    def without_driver_fields(entry):
        return {k: v for k, v in entry.items() if k not in DRIVER_ONLY}

    assert without_driver_fields(live) == without_driver_fields(hindcast)
    assert without_driver_fields(live) == {**{k: v for k, v in body.items() if k != "type"},
                                           "type": body["type"], "ledger": lg.name, "bundle": "dev", "code": CODE}
    assert (hindcast["kind"], hindcast["run_id"]) == ("hindcast", "dev-run-1")

"""U6 and U7 parity: one claim body and one score body, whichever driver records them.

The live/exploratory driver and the hindcast driver (U9) both turn a
``claim_core`` body into a ledger entry through ``claims.entry``, and a
``score_core`` body through ``scoring.entry``; the entries may differ only in
``kind``, ``run_id`` and the IDs built from them.
"""

from datetime import UTC, datetime

import pytest

from claim_fixtures import CODE, MONDAY, TOTALS_30_OF_51, make_ledger, record
from khetru_evidence import claims, forecasts as fc, obs_core, score_core, semantics
from khetru_evidence import scoring

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


def test_live_and_hindcast_scores_differ_only_in_kind_and_run(tmp_path):
    lg = make_ledger(tmp_path / "mandi-wheat", datetime(2026, 10, 19, 10, 30, tzinfo=UTC))
    rel, _ = fc.write_record(lg.root, record(TOTALS_30_OF_51))
    bundle = claims.Bundle(lg.root, "dev")
    [claim_body] = claims.bodies(lg.root, bundle, MONDAY, lg.root / rel)
    slots = [claims.entry(claim_body, ledger=lg.name, kind=kind, bundle="dev", code=CODE, run_id=run_id)
             for kind, run_id in (("exploratory", None), ("hindcast", "dev-run-1"))]
    window = obs_core.imd_dates(semantics.schedule(bundle.sem, MONDAY))

    def score(slot):
        body = score_core.score_claim(
            sem=bundle.sem, threshold_mm=bundle.rule.threshold_mm, slot=slot, voided=False,
            band=dict.fromkeys(window, 2.0), vintage="final-r20270601", climatology=obs_core.Climatology(0.3, 3, 10),
            timing_test=score_core.FORECAST_AVAILABILITY, timing_result="on_time", evidence={})
        return body, scoring.entry(body, slot=slot, code=CODE)

    (live_body, live), (hindcast_body, hindcast) = (score(slot) for slot in slots)

    assert live_body == hindcast_body
    assert not {*DRIVER_ONLY, "claim_id"} & set(live_body)
    differing = {k for k in live.keys() | hindcast.keys() if live.get(k) != hindcast.get(k)}
    assert differing == {*DRIVER_ONLY, "claim_id"}
    assert (hindcast["kind"], hindcast["run_id"], hindcast["outcome"]) == ("hindcast", "dev-run-1", "held")

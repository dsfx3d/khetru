"""U7: Brier score and Brier skill score against an independent library (`scores`, dev only)."""

from datetime import date, timedelta

import numpy as np
import pytest
import xarray as xr
from scores.probability import brier_score

from khetru_evidence import obs_core as O, score_core as S

TOLERANCE = 1e-12


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_brier_and_bss_agree_with_the_scores_library(seed):
    rng = np.random.default_rng(seed)
    n = 500
    probability = rng.uniform(0.01, 0.99, n).round(6)
    climatology = rng.uniform(0.05, 0.6, n).round(6)
    held = rng.random(n) < 0.3
    day = date(2000, 1, 3)
    scored = [
        {"issue_date": (day + timedelta(days=7 * i)).isoformat(), "band": "district",
         "outcome": O.HELD if held[i] else O.NOT_HELD,
         "probability": float(probability[i]), "climatology_probability": float(climatology[i])}
        for i in range(n)
    ]

    rows = S.rows(scored)

    observed = xr.DataArray(held.astype(float), dims="claim")
    theirs = float(brier_score(xr.DataArray(probability, dims="claim"), observed))
    reference = float(brier_score(xr.DataArray(climatology, dims="claim"), observed))
    assert sum(r.brier for r in rows) / n == pytest.approx(theirs, abs=TOLERANCE)
    assert sum(r.brier_climatology for r in rows) / n == pytest.approx(reference, abs=TOLERANCE)
    assert S.bss(rows) == pytest.approx(1 - theirs / reference, abs=TOLERANCE)

"""R2 synthetic registry, exact secondary arithmetic, Monday timing and no future permit retention."""

import copy
from dataclasses import replace

import numpy as np
import pytest

from research.decision_view import Decision
from research.engine import Costs, Leg, Pending, Rejected, Signal, number
from research.experiments.contract import protocol
from research.experiments.registry import build_registry
from research.experiments.runner import ExperimentEngine, mock_inputs
from research.experiments.screen import screen
from research.experiments.strategy import model_at
from research.portfolio import DAY
from research.statistics.fixtures import asof


@pytest.fixture(scope="module")
def r2():
    inputs = mock_inputs()
    registry = build_registry(inputs[0], inputs[1], inputs[2], inputs[3], universe_verified=True)
    entry = registry[0]
    view = asof(inputs[1], entry["decision_ts"] // DAY - 1)
    batch = screen(view, entry["family"], origin="synthetic_test")
    return inputs, registry, batch


def test_metadata_registry_precedes_pvalues_and_exact_integers(r2):
    _, registry, _ = r2
    assert len(registry) == 7
    for entry in registry:
        assert entry["decision_ts"] // DAY % 7 == 4 and entry["decision_ts"] % DAY == 0
        assert entry["N"] == 34 and entry["L"] == 74
        assert entry["expected_counts"] == {"raw": 1500, "primary_holm": 1056, "BY_EG": 204}
        assert len(entry["primary_holm_ids"]) == 1056 and len(entry["BY_EG_ids"]) == 204
        assert "pvalue" not in entry and entry["no_statistics_or_returns_used"]


def test_unknown_metadata_registry_has_unknown_counts_and_explicit_slots(r2):
    inputs, _, _ = r2
    meta = copy.deepcopy(inputs[2])
    meta["Y"][0]["verified"] = False
    registry = build_registry(inputs[0], inputs[1], meta, inputs[3], universe_verified=True)
    assert all(e["status"] == "unavailable" and e["N"] is None for e in registry)
    assert all(e["pool"]["unknown"] and e["excluded_slots"] for e in registry)


def test_secondary_exact_56_returns_28_sum_56_endpoints_numerical_reference(r2):
    inputs, registry, batch = r2
    ts = registry[0]["decision_ts"]
    frame = replace(asof(inputs[1], ts // DAY - 1), ts=ts)
    candidate = next(c for c in batch["family"] if c["id"] == "basket_Y")
    model = model_at(frame, candidate, protocol()["configs"][-1], batch)
    rows = [r for _, r in sorted(frame.observations.items())][-84:]
    values = np.asarray([[np.log(float(r.prices[s])) for s in model.symbols] for r in rows])
    returns = np.diff(values, axis=0)
    x = np.column_stack((np.ones(56), returns[-56:, 1:]))
    coeff = np.linalg.lstsq(x, returns[-56:, 0], rcond=None)[0]
    residual = returns[:, 0] - coeff[0] - returns[:, 1:] @ coeff[1:]
    sums = np.asarray([np.sum(residual[i : i + 28]) for i in range(56)])
    assert np.allclose(model.beta, coeff[1:], rtol=1e-8, atol=1e-10)
    assert abs(model.mean - float(sums.mean())) < 1e-10
    assert abs(model.sd - float(sums.std(ddof=1))) < 1e-10
    current, previous = frame.observations[ts - 1], frame.observations[ts - DAY - 1]
    expected_z = float((sums[-1] - sums.mean()) / sums.std(ddof=1))
    assert abs(model.zscore(current.prices, previous.prices, frame.observations, ts) - expected_z) < 1e-8
    with pytest.raises(Rejected):
        model.zscore(current.prices, previous.prices)
    with pytest.raises(Rejected):
        model.price.zscore(current.prices, origin="real_market")


def test_monday_signal_strictly_next_tuesday_execution(r2):
    (market, _, _, turnover), registry, _ = r2
    monday = registry[0]["decision_ts"]
    leg = Leg("MOCK_timing_only", "W")

    def decide(frame):
        if frame.ts == monday:
            return Decision((Signal("monday", frame.ts, {leg: number("0.1")}),))
        return Decision()

    result = ExperimentEngine(market, turnover, Costs(number(5), number(5))).run_causal(decide)
    fill = next(r for r in result["ledger"] if r["event"] == "fill")
    assert fill["known_at"] == monday and fill["ts"] == monday + DAY


def test_secondary_missing_or_future_available_endpoint_refuses(r2):
    inputs, registry, batch = r2
    ts = registry[0]["decision_ts"]
    frame = replace(asof(inputs[1], ts // DAY - 1), ts=ts)
    candidate = batch["family"][0]
    rows = dict(frame.observations)
    for t in sorted(rows)[:-83]:
        del rows[t]
    with pytest.raises(Rejected):
        model_at(replace(frame, observations=rows), candidate, protocol()["configs"][-1], batch)


def test_capacity_does_not_use_volume_observed_between_decision_and_fill(r2):
    (market, _, _, turnover), registry, _ = r2
    monday = registry[0]["decision_ts"]
    frame = next(f for f in market.frames if f.ts == monday + DAY)
    volume = copy.deepcopy(turnover)
    for row in volume["W"].values():
        row["volCcyQuote"] = "100000"
    volume["W"][monday + DAY - 1]["volCcyQuote"] = "999999999999"
    engine = ExperimentEngine(market, volume, Costs(number(5), number(5)))
    engine.execute(
        frame,
        Pending(frame.ts, "MOCK_capacity_asof", monday, Leg("mock", "W"), number(150) / frame.execution["W"]),
    )
    assert [r["event"] for r in engine.ledger] == ["rejected_capacity"]
    assert engine.ledger[-1]["limit"] == number(100)

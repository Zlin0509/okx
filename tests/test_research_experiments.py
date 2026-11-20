"""Synthetic-only integration and negative tests. No real inputs or historical cache fixtures."""

import copy
from dataclasses import replace

import pytest

from research.engine import Rejected
from research.experiments.contract import protocol
from research.experiments.runner import mock_inputs, run_configuration, validate_adapter
from research.experiments.screen import bind_family, screen
from research.experiments.universe import candidate_family, pool_at
from research.statistics.fixtures import asof, changed_future


@pytest.fixture(scope="module")
def experiment():
    inputs = mock_inputs()
    _, series, meta, turnover = inputs
    view = asof(series, 512)
    pool = pool_at(view.ts, meta, turnover, view.observations, universe_verified=True)
    family = candidate_family(pool["members"], ("W", "X"))
    return inputs, family, screen(view, family, origin="synthetic_test")


def test_complete_candidate_configuration_family_bound_and_no_truth(experiment):
    _, family, batch = experiment
    assert len(family) == 34 and len(batch["permits"]) == 204
    assert len(batch["tests"]) == 1500
    assert batch["status"] == "complete"
    assert "truth" not in batch and "generator" not in batch
    assert set(batch["Holm_FWER"]) == set(batch["tests"])


def test_incomplete_or_nan_family_cannot_license(experiment):
    _, family, batch = experiment
    missing = copy.deepcopy(batch["analyses"])
    del missing[family[0]["id"]]
    with pytest.raises(Rejected):
        bind_family(missing, {}, family, batch["decision_ts"])
    invalid = copy.deepcopy(batch["analyses"])
    next(iter(invalid[family[0]["id"]]["tests"].values()))["pvalue"] = float("nan")
    with pytest.raises(Rejected):
        bind_family(invalid, {}, family, batch["decision_ts"])


def test_unavailable_member_retained_and_rejects_entire_family(experiment):
    _, family, batch = experiment
    analyses = copy.deepcopy(batch["analyses"])
    missing = family[0]["id"]
    del analyses[missing]
    result = bind_family(analyses, {missing: {"reason": "missing_data"}}, family, batch["decision_ts"])
    assert result["status"] == "unavailable" and result["unavailable_test_ids"]
    assert all(p["status"] == "rejected" for p in result["permits"].values())
    assert len(result["permits"]) == 204


@pytest.mark.parametrize("fault", ["funding", "mark", "state_snapshot", "real"])
def test_actual_cost_adapter_missing_or_snapshot_input_refuses(experiment, fault):
    (market, *_), _, _ = experiment
    kind, origin = "synthetic_settlement", "synthetic_test"
    if fault == "funding":
        rates = dict(market.realized_rates)
        del rates[next(iter(rates))]
        market = replace(market, realized_rates=rates)
    elif fault == "mark":
        frames = list(market.frames)
        frames[0] = replace(frames[0], marks={})
        market = replace(market, frames=tuple(frames))
    elif fault == "state_snapshot":
        kind = "WS_state_snapshot"
    else:
        origin = "real_market"
    with pytest.raises(Rejected):
        validate_adapter(market, funding_kind=kind, origin=origin)


def test_next_execution_costs_reconcile_and_pressure_is_full_rerun(experiment):
    inputs, _, batch = experiment
    cfg = protocol()["configs"][0]
    base = run_configuration(cfg, batch, inputs, "base")
    stress = run_configuration(cfg, batch, inputs, "stress")
    fills = [r for r in base["accounting"]["ledger"] if r["event"] in {"fill", "risk_fill"}]
    assert fills, "Keep this fixed fixture even if later versions refuse; never tune to force profit"
    assert all(r["ts"] > r["known_at"] for r in fills)
    assert base["accounting"]["reconciliation_error"] == "0.000000000000000000000000000000"
    assert base["real_returns"] is None and base["annualized_metrics"] is None
    assert all(r["fee"] is not None for r in base["cost_ledger"] if r["event"] == "fill")
    assert stress["costs"] == {"fee_bps": 10, "slippage_bps": 10}
    assert base["costs"] == {"fee_bps": 5, "slippage_bps": 5}


def test_future_changes_do_not_change_past_signals_or_complete_ledger_prefix(experiment):
    inputs, _, batch = experiment
    cfg = protocol()["configs"][0]
    original = run_configuration(cfg, batch, inputs, "base")
    altered = mock_inputs(changed_future(inputs[1], 540 * 86400000 + 86400000 - 1))
    changed = run_configuration(cfg, batch, altered, "base")
    cutoff = 540 * 86400000 + 86400000 - 1
    for key in ("signals",):
        assert [r for r in original[key] if r["ts"] <= cutoff] == [
            r for r in changed[key] if r["ts"] <= cutoff
        ]
    assert [r for r in original["accounting"]["ledger"] if r["ts"] <= cutoff] == [
        r for r in changed["accounting"]["ledger"] if r["ts"] <= cutoff
    ]

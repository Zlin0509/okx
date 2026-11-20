"""Synthetic-only integration and negative tests. No real inputs or historical cache fixtures."""

import copy
import json
from dataclasses import replace

import pytest

from research.engine import Costs, Leg, Pending, Rejected, number
from research.experiments.contract import protocol
from research.experiments.runner import ExperimentEngine, mock_inputs, run_configuration, validate_adapter
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
    assert abs(number(base["accounting"]["reconciliation_error"])) < number("1e-24")
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


def test_changed_training_or_extra_configuration_cannot_reuse_screen(experiment):
    inputs, _, batch = experiment
    cfg = protocol()["configs"][0]
    changed = mock_inputs(changed_future(inputs[1], 300 * 86400000))
    with pytest.raises(Rejected, match="historical observations"):
        run_configuration(cfg, batch, changed, "base")
    with pytest.raises(Rejected):
        run_configuration({**cfg, "entry_z": 0.1}, batch, inputs, "base")


def test_serialized_family_preserves_same_synthetic_models_and_results(experiment):
    inputs, _, batch = experiment
    cfg = protocol()["configs"][0]
    restored = json.loads(json.dumps(batch))
    original = run_configuration(cfg, batch, inputs, "base")
    assert run_configuration(cfg, restored, inputs, "base") == original
    entries = [e for e in original["signals"] if e["action"] == "entry"]
    assert entries
    assert entries[0]["model"]["origin"] == "synthetic_test"
    assert entries[0]["model"]["price"]["provenance"]["screen_only"] is True


def test_shared_symbol_opposing_legs_do_not_net_capacity(experiment):
    (market, _, _, turnover), _, _ = experiment
    volumes = copy.deepcopy(turnover)
    for row in volumes["W"].values():
        row["volCcyQuote"] = "100000"
    engine = ExperimentEngine(market, volumes, Costs(number(5), number(5)))
    frame = market.frames[0]
    quantity = number(60) / frame.execution["W"]
    engine.execute(frame, Pending(frame.ts, "one", frame.ts - 1, Leg("one", "W"), quantity))
    engine.execute(frame, Pending(frame.ts, "two", frame.ts - 1, Leg("two", "W"), -quantity))
    assert [r["event"] for r in engine.ledger] == ["fill", "rejected_capacity"]
    assert engine.ledger[-1]["reason"] == "absolute_daily_participation"

"""Public interface negative tests and synthetic-only review regressions."""

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from pathlib import Path

import pytest

from research.causal import CausalEngine
from research.decision_view import Decision, DecisionSnapshot
from research.engine import Leg, Rejected, Signal, number
from research.m3 import config, inputs, render, run_case
from research.portfolio import DAY, Portfolio, allocate
from research.risk_trace import exposure_trace
from research.spreads import HOLM_ALPHA, Model, holm


def test_public_callback_active_future_read_enumeration_and_write_fail():
    market, observations, *_ = inputs("convergence")
    captured = []

    def hostile(view):
        assert isinstance(view, DecisionSnapshot)
        # Explicit future lookup fails, enumeration reveals no future/unavailable entries.
        with pytest.raises(KeyError):
            view.observations[market.frames[-1].ts + DAY]
        for name in ("engine", "market", "frames", "provider", "ledger", "source", "__dict__"):
            with pytest.raises(AttributeError):
                getattr(view, name)
        assert all(ts <= view.ts and row.available_at <= view.ts for ts, row in view.observations.items())
        assert all(fill.ts <= view.ts and fill.known_at < fill.ts for fill in view.fills)
        assert all(p.known_at <= view.ts for p in view.pending)
        with pytest.raises((FrozenInstanceError, AttributeError)):
            view.ts = 10**20
        with pytest.raises(TypeError):
            view.marks["A"] = Decimal(0)
        with pytest.raises(TypeError):
            view.execution["A"] = Decimal(0)
        with pytest.raises(TypeError):
            view.observations[view.ts + DAY] = None
        with pytest.raises(TypeError):
            view.positions[Leg("bad", "A")] = None
        for observation in view.observations.values():
            with pytest.raises(TypeError):
                observation.prices["A"] = Decimal(0)
            with pytest.raises((FrozenInstanceError, AttributeError)):
                observation.available_at = 0
            with pytest.raises(TypeError):
                vars(observation)
            break
        for leg, held in view.positions.items():
            with pytest.raises((FrozenInstanceError, AttributeError)):
                held.quantity = 0
            with pytest.raises((FrozenInstanceError, AttributeError)):
                leg.symbol = "future"
            with pytest.raises(TypeError):
                vars(leg)
        for intent in view.pending:
            with pytest.raises(AttributeError):
                _ = intent.ts  # No future fill time/calendar in this interface.
            with pytest.raises((FrozenInstanceError, AttributeError)):
                intent.target = 0
        for fill in view.fills:
            with pytest.raises((FrozenInstanceError, AttributeError)):
                fill.executed_delta = 0
        captured.append(view)
        if view.ts == 57 * DAY - 1:
            return Decision(
                (Signal("known", view.ts, {Leg("p", "A"): number(1)}, delay_steps={Leg("p", "A"): 2}),)
            )
        return Decision()

    CausalEngine(market).run_causal(hostile, observations)
    assert captured[0].observations == {}
    assert not captured[0].positions and not captured[0].fills
    assert len(captured[-1].observations) == 160
    assert max(captured[2].observations) <= captured[2].ts
    assert {f.name for f in fields(captured[0])} == {
        "ts",
        "marks",
        "execution",
        "observations",
        "positions",
        "fills",
        "pending",
        "equity",
    }


def test_not_yet_available_observation_is_not_enumerable_then_appears():
    market, observations, *_ = inputs("convergence")
    ts = 57 * DAY - 1
    observations[ts] = replace(observations[ts], available_at=ts + DAY)
    views = {}

    def decide(view):
        if ts <= view.ts <= ts + DAY:
            views[view.ts] = view
        return Decision()

    CausalEngine(market).run_causal(decide, observations)
    assert ts not in views[ts].observations
    with pytest.raises(KeyError):
        views[ts].observations[ts]
    assert ts in views[ts + DAY].observations
    assert ts not in views[ts].observations  # Retained snapshots never grow.


def test_source_mutation_cannot_change_captured_observation_copy():
    market, observations, *_ = inputs("convergence")
    captured = []

    def decide(view):
        if view.ts == DAY - 1:
            captured.append(view)
        return Decision()

    CausalEngine(market).run_causal(decide, observations)
    original = captured[0].observations[DAY - 1].prices["A"]
    observations[DAY - 1].prices["A"] = Decimal(999)
    assert captured[0].observations[DAY - 1].prices["A"] == original


def test_portfolio_does_not_receive_or_retain_source_or_engine():
    _, observations, relations, stress = inputs("convergence")
    portfolio = Portfolio(relations, config(), stress)
    assert not {"observations", "history", "market", "engine", "provider"}.intersection(vars(portfolio))
    assert all(value is not observations for value in vars(portfolio).values())


@pytest.mark.parametrize("proposals", [{}, {"empty": {}}, {"zero": {"A": Decimal(0)}}])
def test_empty_or_all_zero_allocation(proposals):
    assert allocate(proposals, {}, Decimal(10000), config()) == ({}, Decimal(0))


def test_zero_beta_and_mixed_empty_proposals_never_divide_by_zero():
    model = Model(("Y", "X"), 0.0, (0.0,), 0.0, 1.0, 0, 0, 0, None, None)
    proposal = {s: number(weight) * 4000 for s, weight in zip(model.symbols, model.vector, strict=True)}
    allocated, scale = allocate(
        {"pair": proposal, "empty": {}, "zero": {"Q": Decimal(0)}}, {}, Decimal(10000), config()
    )
    assert scale == Decimal("0.6")
    assert allocated == {"pair": {"Y": Decimal(2400), "X": Decimal(0)}}


def test_frozen_alpha_and_pending_tests_are_explicit():
    assert config()["holm_alpha"] == HOLM_ALPHA == 0.05
    assert holm({"one": 0.04}, ["one"])["one"]["rejected_null"]
    summary = json.loads(render()["summary.json"])
    assert any("relative-return" in name for name in summary["qualification_tests_pending"])
    assert any("qualification wiring" in name for name in summary["qualification_tests_pending"])
    assert any("FDR" in name for name in summary["qualification_tests_pending"])


def test_risk_reductions_remain_flagged_until_restored_with_duration_and_event_order():
    run = run_case("hard_drift")
    trace = run["risk_exposure"]
    reductions = [r for r in trace["events"] if r["risk_exit_fill"]]
    assert reductions[0]["hard_limit_breached"]
    assert number(reductions[0]["asset_excess"]["B"]) > 0
    assert not reductions[-1]["hard_limit_breached"]
    assert all(r["reducing_existing_leg"] for r in reductions)
    episode = trace["episodes"][0]
    assert episode["restored_seq"] > reductions[0]["seq"]
    assert episode["duration_ms"] == 1  # Abstract close at DAY-1 to next open: not latency evidence.
    assert episode["restored_ts"] == reductions[-1]["ts"]
    assert not episode["right_censored"]
    assert episode["breached_event_count"] > 1
    prefix = [r for r in run["accounting"]["ledger"] if r["seq"] <= reductions[0]["seq"]]
    assert exposure_trace(prefix)["episodes"][-1]["right_censored"]
    for row in trace["events"]:
        if row["reducing_existing_leg"] is False:
            assert not row["hard_limit_breached"]


def test_legacy_callback_shape_and_inherited_engine_entry_refuse():
    market, *_ = inputs("convergence")
    with pytest.raises(TypeError):
        CausalEngine(market).run_causal(lambda frame, engine, pending: Decision())
    with pytest.raises(Rejected, match="immutable snapshot"):
        CausalEngine(market).run()


def test_breach_duration_tracks_a_delayed_risk_exit_without_relaxing_cap():
    market, observations, relations, stress = inputs("hard_drift")
    delayed = 58 * DAY
    calendar = {
        **market.execution_calendar,
        "B": tuple(t for t in market.execution_calendar["B"] if t != delayed),
    }
    frames = tuple(
        replace(frame, execution={s: p for s, p in frame.execution.items() if s != "B"})
        if frame.ts == delayed
        else frame
        for frame in market.frames
    )
    run = run_case(
        "hard_drift",
        (replace(market, frames=frames, execution_calendar=calendar), observations, relations, stress),
    )
    episode = run["risk_exposure"]["episodes"][0]
    assert episode["duration_ms"] == DAY + 1
    assert episode["restored_ts"] == 59 * DAY
    assert any(
        row["hard_limit_breached"] and row["ts"] == 59 * DAY - 1 for row in run["risk_exposure"]["events"]
    )


def test_ci_uses_synthetic_guard_and_m3_not_real_cache_audits():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text()
    assert "research.synthetic_guard pytest" in workflow
    assert "research.m3 verify" in workflow
    assert "research.verify_m2" not in workflow and "research.data_audit verify" not in workflow


def test_snapshot_guard_with_mock_cache_only(tmp_path):
    # Isolated subprocess: no real snapshot path is opened, not even for this negative test.
    code = """
import pathlib, sys
import research.synthetic_guard as guard
guard.ROOT = pathlib.Path(sys.argv[1])
cache = guard.ROOT / 'research/snapshots/mock'
cache.mkdir(parents=True)
target = cache / 'test.body'
target.write_text('SYNTHETIC MOCK ONLY')
report = guard.install()
try:
    target.read_bytes()
except PermissionError as error:
    assert str(error) == 'synthetic_only_snapshot_forbidden'
else:
    raise AssertionError('guard failed')
assert report['blocked_snapshot_accesses'] == 1
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

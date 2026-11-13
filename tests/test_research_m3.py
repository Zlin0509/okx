"""Synthetic-only contract tests, including causal invariance and shared-asset risk."""

import math
from dataclasses import replace
from decimal import Decimal

import pytest

from research.causal import CausalEngine
from research.decision_view import Decision
from research.engine import Costs, Engine, Frame, Leg, Rejected, Signal, number, serializable
from research.m3 import CASES, assert_behaviors, config, inputs, run_case
from research.portfolio import DAY, allocate, holding_periods
from research.spreads import Observation, Qualification, fit_model, holm


def truth(symbols=("Y", "X")):
    return Qualification(symbols, 0, "qualified", "synthetic_fixture_truth")


def known_history(basket=False):
    rows = []
    for day in range(56):
        x = math.sin(day * 0.19) * 0.2 + 3
        w = math.cos(day * 0.31) * 0.1 + 2
        noise = math.sin(day * 1.7) * 0.001
        y = 0.4 + 1.2 * x + (0.7 * w if basket else 0) + noise
        rows.append(
            Observation(
                day * DAY + DAY - 1,
                day * DAY + DAY - 1,
                {"Y": math.exp(y), "X": math.exp(x), "W": math.exp(w)},
            )
        )
    return rows


@pytest.mark.parametrize("basket", [False, True])
def test_known_pair_and_basket_hedge_coefficients(basket):
    symbols = ("Y", "X", "W") if basket else ("Y", "X")
    model = fit_model(known_history(basket), symbols, 57 * DAY - 1, truth(symbols))
    assert model.beta[0] == pytest.approx(1.2, abs=0.003)
    if basket:
        assert model.beta[1] == pytest.approx(0.7, abs=0.003)
    assert model.intercept == pytest.approx(0.4, abs=0.01)
    assert model.train_end == 56 * DAY - 1
    assert model.train_end < model.available_at
    assert model.sd > 0
    assert model.half_life_days is None or model.half_life_days > 0


def test_training_excludes_current_and_future_observations():
    history = known_history()
    decision = 57 * DAY - 1
    baseline = fit_model(history, ("Y", "X"), decision, truth())
    modified = history + [
        Observation(decision, decision, {"Y": 1e30, "X": 1e-20}),
        Observation(decision + DAY, decision + DAY, {}),
    ]
    assert fit_model(modified, ("Y", "X"), decision, truth()) == baseline


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "missing_observation"),
        ("gap", "missing_observation"),
        ("stale", "missing_observation"),
        ("unavailable", "unavailable_training"),
        ("zero", "invalid_price"),
        ("constant", "singular_training"),
        ("exact", "zero_volatility"),
    ],
)
def test_training_rejects_unusable_windows(change, code):
    history = known_history()
    decision = 57 * DAY - 1
    if change == "missing":
        history[10] = replace(history[10], prices={})
    if change == "gap":
        history[10] = replace(history[10], ts=history[10].ts + 1, available_at=history[10].available_at + 1)
    if change == "stale":
        history = [replace(row, ts=row.ts - DAY, available_at=row.available_at - DAY) for row in history]
    if change == "unavailable":
        history[10] = replace(history[10], available_at=decision + 1)
    if change == "zero":
        history[10] = replace(history[10], prices={"Y": 0, "X": 3})
    if change == "constant":
        history = [replace(row, prices={"Y": row.prices["Y"], "X": 3}) for row in history]
    if change == "exact":
        history = [
            replace(row, prices={"Y": row.prices["X"] ** 1.2, "X": row.prices["X"]}) for row in history
        ]
    with pytest.raises(Rejected) as caught:
        fit_model(history, ("Y", "X"), decision, truth())
    assert caught.value.code == code


@pytest.mark.parametrize(
    "qualification",
    [
        Qualification(("Y", "X"), 0),
        Qualification(("Y", "X"), 0, "qualified", "correlation"),
        Qualification(("Y", "X"), 99 * DAY, "qualified", "synthetic_fixture_truth"),
        Qualification(("Y", "W"), 0, "qualified", "synthetic_fixture_truth"),
    ],
)
def test_qualification_unknown_unavailable_or_wrong_binding_refuses(qualification):
    with pytest.raises(Rejected, match="synthetic truth"):
        fit_model(known_history(), ("Y", "X"), 57 * DAY - 1, qualification)


def test_holm_requires_complete_prespecified_family_and_does_not_qualify():
    result = holm({"a": 0.01, "b": 0.03, "c": 0.2}, ["a", "b", "c"])
    assert result["a"]["adjusted_p"] == pytest.approx(0.03)
    assert result["b"]["adjusted_p"] == pytest.approx(0.06)
    assert not result["b"]["rejected_null"]
    with pytest.raises(Rejected):
        holm({"a": 0.01}, ["a", "b"])
    with pytest.raises(Rejected):
        holm({"a": float("nan")}, ["a"])


@pytest.fixture(scope="module")
def runs():
    return {case: run_case(case) for case in CASES}


def test_registered_synthetic_behaviors_and_reconciliation(runs):
    assert_behaviors(runs)
    for run in runs.values():
        assert not run["performance_evidence"]
        assert abs(number(run["accounting"]["reconciliation_error"])) < Decimal("1e-24")


def test_actual_held_period_and_daily_exit_need_not_be_two_weeks(runs):
    run = runs["convergence"]
    first = run["holdings"][0]
    assert first["first_fill"] == 57 * DAY
    assert first["flat_at"] == 59 * DAY
    assert first["held_days"] == 2
    exit_event = next(e for e in run["signals"] if e["action"] == "exit")
    assert exit_event["ts"] // DAY % 7 != 0
    assert not first["right_censored"]


def test_timeout_uses_actual_first_fill_and_relationship_failure_invalidates(runs):
    timeout = next(e for e in runs["nonconvergence"]["signals"] if e["action"] == "exit")
    assert timeout["reason"] == "timeout"
    assert timeout["ts"] - 57 * DAY >= 28 * DAY
    broken = runs["relationship_break"]
    assert sum(e["action"] == "entry" for e in broken["signals"]) == 1
    assert any(e["reason"] == "relationship_invalidated" for e in broken["signals"])


def test_shared_asset_allocation_counts_opposite_books_without_netting():
    p = config()
    proposals = {
        "p": {"A": Decimal(2000), "B": Decimal(-2000)},
        "q": {"A": Decimal(-2000), "C": Decimal(2000)},
    }
    allocations, scale = allocate(proposals, {}, Decimal(10000), p)
    assert scale == Decimal("0.6")
    assert abs(allocations["p"]["A"]) + abs(allocations["q"]["A"]) == 2400
    assert allocations["p"]["A"] + allocations["q"]["A"] == 0
    _, blocked = allocate(proposals, {Leg("old", "A"): Decimal(2500)}, Decimal(10000), p)
    assert blocked == 0


def test_shared_realized_exposure_and_strict_next_open(runs):
    run = runs["shared_asset"]
    first_entries = [e for e in run["signals"] if e["action"] == "entry" and e["ts"] // DAY == 56]
    assert len(first_entries) == 2
    assert all(number(e["allocation_scale"]) < 1 for e in first_entries)
    for row in run["accounting"]["ledger"]:
        if row["event"] == "fill":
            assert row["ts"] % DAY == 0
            assert row["ts"] > row["known_at"]
            assert number(row["gross_notional"]) <= number(row["equity"])
            assert max(map(number, row["asset_absolute_notional"].values())) <= number(row["equity"]) / 4
            b_sum = sum(abs(number(p["quantity"])) for p in row["positions"] if p["symbol"] == "B")
            if b_sum:
                assert number(row["asset_absolute_notional"]["B"]) > 0


@pytest.mark.parametrize("case", ["delayed_leg", "partial_leg", "unfilled_leg"])
def test_incomplete_hedge_cancels_pending_and_flattens_next_open(runs, case):
    run = runs[case]
    assert run["holdings"][0]["flat_at"] == 58 * DAY
    assert any(e["reason"] == "hedge_failure" for e in run["signals"])
    ledger = run["accounting"]["ledger"]
    assert any(e["event"] == "cancel_book_pending" for e in ledger)
    if case == "delayed_leg":
        assert not any(e["event"] == "fill" and e.get("symbol") == "B" and e["ts"] < 80 * DAY for e in ledger)
        assert any(e["event"] == "cancel_book_pending" and e["cancelled_pending"] == 1 for e in ledger)


def test_hard_drift_cancels_and_exits_only_next_tick(runs):
    ledger = runs["hard_drift"]["accounting"]["ledger"]
    halt = next(e for e in ledger if e["event"] == "risk_halt")
    exits = [e for e in ledger if e["event"] == "risk_fill"]
    assert exits and all(e["ts"] > halt["ts"] for e in exits)
    assert not runs["hard_drift"]["accounting"]["risk_exit_unresolved"]


@pytest.mark.parametrize("case", ["convergence", "shared_asset", "delayed_leg"])
def test_changing_future_data_cannot_change_past_signals_or_positions(case):
    original = run_case(case)
    market, observations, relations, stress = inputs(case)
    cutoff = 80 * DAY + DAY - 1
    frames = tuple(
        replace(
            f,
            marks={s: price * Decimal("1.08") for s, price in f.marks.items()},
            execution={s: price * Decimal("1.08") for s, price in f.execution.items()},
        )
        if f.ts > cutoff
        else f
        for f in market.frames
    )
    rates = {(ts, s): -rate * 2 if ts > cutoff else rate for (ts, s), rate in market.realized_rates.items()}
    observations = {
        ts: replace(row, prices={s: price * Decimal("1.08") for s, price in row.prices.items()})
        if ts > cutoff
        else row
        for ts, row in observations.items()
    }
    changed = run_case(
        case, (replace(market, frames=frames, realized_rates=rates), observations, relations, stress)
    )
    assert [e for e in original["signals"] if e["ts"] <= cutoff] == [
        e for e in changed["signals"] if e["ts"] <= cutoff
    ]
    assert [e for e in original["accounting"]["ledger"] if e["ts"] <= cutoff] == [
        e for e in changed["accounting"]["ledger"] if e["ts"] <= cutoff
    ]


def test_stream_adapter_matches_frozen_r2_for_identical_past_known_orders():
    market, *_ = inputs("convergence")
    t = 57 * DAY - 1
    signals = [
        Signal("open", t, {Leg("p", "A"): number(1), Leg("p", "B"): number(-2)}),
        Signal("close", t + 3 * DAY, {Leg("p", "A"): number(0), Leg("p", "B"): number(0)}),
    ]
    a = Engine(market, costs=Costs()).run(signals)
    b = CausalEngine(market, costs=Costs()).run_causal(
        lambda f: Decision(tuple(s for s in signals if s.known_at == f.ts))
    )
    assert serializable(a["ledger"]) == serializable(b["ledger"])
    assert a["cash"] == b["cash"]
    assert a["funding_cashflow"] == b["funding_cashflow"]
    assert a["fees"] == b["fees"]


def test_real_provenance_refuses_before_callback():
    market, *_ = inputs("convergence")
    engine = CausalEngine(replace(market, provenance="okx_m1_snapshot"))
    with pytest.raises(Rejected) as caught:
        engine.run_causal(lambda *args: pytest.fail("Real data callback must never run"))
    assert caught.value.code == "real_window_disabled"
    assert not engine.ledger


def test_missing_future_market_data_refuses_before_accounting():
    market, *_ = inputs("convergence")
    frames = list(market.frames)
    frames[-1] = Frame(frames[-1].ts, {}, {})
    engine = CausalEngine(replace(market, frames=tuple(frames)))
    with pytest.raises(Rejected):
        engine.run_causal(lambda *_: Decision())
    assert not engine.ledger


def test_open_holding_period_is_censored(runs):
    ledger = runs["convergence"]["accounting"]["ledger"]
    prefix = [e for e in ledger if e["ts"] <= 58 * DAY - 1]
    periods = holding_periods(prefix)
    assert periods[0]["right_censored"] and periods[0]["flat_at"] is None


def test_current_observation_availability_and_book_identity():
    market, observations, relations, stress = inputs("convergence")
    entry_ts = 57 * DAY - 1
    observations[entry_ts] = replace(observations[entry_ts], available_at=entry_ts + 1)
    run = run_case("convergence", (market, observations, relations, stress))
    assert not any(e["action"] == "entry" and e["ts"] == entry_ts for e in run["signals"])


def test_synthetic_funding_has_both_cashflow_signs_and_costs(runs):
    ledger = runs["convergence"]["accounting"]["ledger"]
    flows = [number(row["cash_delta"]) for row in ledger if row["event"] == "funding"]
    assert any(flow > 0 for flow in flows) and any(flow < 0 for flow in flows)
    assert number(runs["convergence"]["accounting"]["fees"]) > 0


def test_terminal_exit_is_unresolved_and_does_not_fabricate_fill():
    market, *_ = inputs("convergence")
    last = market.frames[-1].ts
    signal = Signal("terminal", last, {Leg("p", "A"): number(0)})
    result = CausalEngine(market).run_causal(lambda f: Decision((signal,)) if f.ts == last else Decision())
    assert result["unresolved_signal_ids"] == ["terminal"]
    assert result["ledger"][-1]["event"] == "unexecutable_signal"

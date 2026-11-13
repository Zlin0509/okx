from dataclasses import replace
from decimal import Decimal

import pytest

from research.engine import (
    Costs,
    Engine,
    Frame,
    Leg,
    Market,
    Rejected,
    Signal,
    fill_position,
    number,
    require_full_research,
)
from research.engine import Position as Holding
from research.m2 import synthetic_inputs

D = Decimal
FREE = Costs(D(0), D(0))


def market(prices, funding=None):
    times = tuple(i * 1000 for i in range(len(prices)))
    symbols = sorted(prices[0])
    funding = funding or {}
    return Market(
        tuple(
            Frame(t, {s: D(str(p)) for s, p in row.items()}, {s: D(str(p)) for s, p in row.items()})
            for t, row in zip(times, prices, strict=True)
        ),
        times,
        {s: times for s in symbols},
        {s: tuple(sorted(t for t, coin in funding if coin == s)) for s in symbols},
        {key: D(str(rate)) for key, rate in funding.items()},
        "synthetic_test",
        "Explicit synthetic test calendar; not real historical returns",
    )


def test_independent_hand_calculated_round_trip_cashflow_and_no_double_slippage():
    raw, data, signals = synthetic_inputs()
    result = Engine(data, costs=Costs(D(10), D(100))).run(signals)
    # Long 2: buy 101, sell 118.8 => 35.6. Short 3: sell 49.5, buy 45.45 => 12.15.
    # Funding: -2*110*.01 + 3*40*(-.02) = -4.6.
    assert result["realized_pnl"] == D("47.75")
    assert result["funding_cashflow"] == D("-4.6")
    assert result["fees"] == D("0.72445")
    assert result["slippage_diagnostic"] == D("7.25")
    assert result["cash"] == result["equity"] == D("10042.42555")
    assert result["unrealized_pnl"] == result["reconciliation_error"] == 0
    assert raw["provenance"] == result["provenance"] == "synthetic_test"
    for row in result["ledger"]:
        assert (
            row["cash"]
            == D(10000) + row["realized_pnl"] + row["cumulative_funding_cashflow"] - row["cumulative_fees"]
        )
        assert row["equity"] == row["cash"] + row["unrealized_pnl"]


@pytest.mark.parametrize(
    ("quantity", "rate", "flow"),
    [(1, ".01", -1), (-1, ".01", 1), (1, "-.01", 1), (-1, "-.01", -1), (1, "0", 0)],
)
def test_positive_negative_and_zero_funding_on_each_side(quantity, rate, flow):
    data = market([{"A": 100}] * 3, {(2000, "A"): rate})
    result = Engine(data, costs=FREE).run([Signal("open", 0, {Leg("pair", "A"): D(quantity)})])
    assert result["funding_cashflow"] == flow
    assert result["equity"] == 10000 + flow


def test_existing_positions_pay_before_same_timestamp_close_new_positions_do_not():
    data = market([{"A": 100}] * 3, {(1000, "A"): ".01", (2000, "A"): ".01"})
    leg = Leg("pair", "A")
    result = Engine(data, costs=FREE).run(
        [Signal("open", 0, {leg: D(1)}), Signal("close", 1500, {leg: D(0)})]
    )
    charges = [r for r in result["ledger"] if r["event"] == "funding"]
    assert [(r["ts"], r["cash_delta"]) for r in charges] == [(2000, D(-1))]
    assert result["cash"] == 9999


def test_short_open_does_not_credit_sale_notional_as_spendable_cash():
    data = market([{"A": 100}] * 2)
    result = Engine(data, costs=FREE).run([Signal("short", 0, {Leg("pair", "A"): D(-2)})])
    assert result["cash"] == 10000
    assert result["net_notional"] == -200


def test_average_entry_partial_close_and_reversal_realize_only_closed_quantity():
    p, realized = fill_position(Holding(D(2), D(100)), D(2), D(120))
    assert (p.quantity, p.average_entry, realized) == (D(4), D(110), D(0))
    p, realized = fill_position(p, D(-1), D(130))
    assert (p.quantity, p.average_entry, realized) == (D(3), D(110), D(20))
    p, realized = fill_position(p, D(-4), D(90))
    assert (p.quantity, p.average_entry, realized) == (D(-1), D(90), D(-60))
    p, realized = fill_position(p, D(1), D(80))
    assert (p.quantity, p.average_entry, realized) == (D(0), D(0), D(10))


def test_signal_never_fills_at_same_timestamp_and_later_prices_do_not_change_prefix():
    data = market([{"A": 100}, {"A": 101}, {"A": 102}, {"A": 103}])
    orders = [Signal("open", 1000, {Leg("pair", "A"): D(1)})]
    original = Engine(data, costs=FREE).run(orders)
    changed = replace(data, frames=(*data.frames[:3], Frame(3000, {"A": D(200)}, {"A": D(200)})))
    altered = Engine(changed, costs=FREE).run(orders)
    fills = [r for r in original["ledger"] if r["event"] == "fill"]
    assert fills[0]["ts"] == 2000
    assert fills[0]["execution_price"] == 102
    assert [r for r in original["ledger"] if r["ts"] < 3000] == [
        r for r in altered["ledger"] if r["ts"] < 3000
    ]


@pytest.mark.parametrize("failure", ["rate", "mark", "execution", "frame", "duplicate_frame"])
def test_missing_data_rejected_before_any_fill_or_cash_mutation(failure):
    data = market([{"A": 100}] * 3, {(2000, "A"): ".01"})
    if failure == "rate":
        data = replace(data, realized_rates={})
    elif failure == "mark":
        data = replace(data, frames=(*data.frames[:2], replace(data.frames[2], marks={})))
    elif failure == "execution":
        data = replace(data, frames=(data.frames[0], replace(data.frames[1], execution={}), data.frames[2]))
    elif failure == "frame":
        data = replace(data, frames=data.frames[:2])
    else:
        data = replace(data, frames=(data.frames[0], data.frames[0], data.frames[2]))
    engine = Engine(data, costs=FREE)
    with pytest.raises(Rejected):
        engine.run([Signal("open", 0, {Leg("pair", "A"): D(1)})])
    assert engine.cash == 10000 and engine.ledger == []


def test_missing_calendar_cannot_be_inferred_from_observed_rates():
    data = market([{"A": 100}] * 2)
    with pytest.raises(Rejected, match="explicit funding calendar"):
        Engine(replace(data, funding_calendar={}), costs=FREE).run()


def test_shared_asset_absolute_exposure_not_netted_between_spreads():
    data = market([{"A": 100}] * 2)
    targets = {Leg("first", "A"): D(10), Leg("second", "A"): D(-10), Leg("third", "A"): D(6)}
    result = Engine(data, costs=FREE).run([Signal("open", 0, targets)])
    assert result["asset_absolute_notional"] == {"A": D(2000)}
    assert result["net_notional"] == 0
    assert len([r for r in result["ledger"] if r["event"] == "rejected_risk"]) == 1


def test_gross_cap_and_post_cost_single_asset_cap():
    symbols = {f"asset{i}": 100 for i in range(8)}
    data = market([symbols] * 2)
    targets = {Leg("basket", s): D(20) for s in symbols}
    result = Engine(data, costs=FREE).run([Signal("open", 0, targets)])
    assert result["gross_notional"] == 10000
    assert len([r for r in result["ledger"] if r["event"] == "rejected_risk"]) == 3
    # A nominal 25% position exceeds the post-fee equity cap and must be refused.
    result = Engine(market([{"A": 100}] * 2), costs=Costs(D(5), D(0))).run(
        [Signal("open", 0, {Leg("pair", "A"): D(25)})]
    )
    assert result["gross_notional"] == 0
    assert any(r["event"] == "rejected_risk" for r in result["ledger"])


def test_price_drift_halts_and_flattens_at_next_execution_not_current_mark():
    data = market([{"A": p} for p in (100, 100, 1000, 900)])
    result = Engine(data, costs=FREE).run([Signal("open", 0, {Leg("pair", "A"): D(20)})])
    halt = next(r for r in result["ledger"] if r["event"] == "risk_halt")
    exit_fill = next(r for r in result["ledger"] if r["event"] == "risk_fill")
    assert halt["ts"] == 2000 and exit_fill["ts"] == 3000
    assert exit_fill["execution_price"] == 900
    assert result["cash"] == 26000 and result["gross_notional"] == 0
    assert result["risk_halted"] and not result["risk_exit_unresolved"]


def test_halt_cancels_stale_second_leg_and_does_not_reopen_it():
    data = market([{"A": a, "B": 100} for a in (100, 100, 1000, 900)])
    a, b = Leg("pair", "A"), Leg("pair", "B")
    result = Engine(data, costs=FREE).run([Signal("open", 0, {a: D(20), b: D(-1)}, delay_steps={b: 2})])
    assert not [r for r in result["ledger"] if r["event"] == "fill" and r["symbol"] == "B"]
    assert result["gross_notional"] == 0


def test_terminal_drift_reports_unresolved_exit_instead_of_fabricated_terminal_fill():
    data = market([{"A": p} for p in (100, 100, 1000)])
    result = Engine(data, costs=FREE).run([Signal("open", 0, {Leg("pair", "A"): D(20)})])
    assert result["risk_exit_unresolved"]
    assert not [r for r in result["ledger"] if r["event"] == "risk_fill"]
    assert result["unrealized_pnl"] == 18000


def test_nonpositive_equity_refuses_result_and_does_not_invent_liquidation_recovery():
    data = market([{"A": p} for p in (100, 100, 1000)])
    with pytest.raises(Rejected) as error:
        Engine(data, costs=FREE).run([Signal("open", 0, {Leg("pair", "A"): D(-20)})])
    assert error.value.code == "insolvent"


@pytest.mark.parametrize("fraction", ["0", "0.5", "1"])
def test_unfilled_partial_and_delayed_hedges_preserve_actual_single_leg_exposure(fraction):
    _, data, signals = synthetic_inputs()
    leg = Leg("pair", "B")
    opening = replace(signals[0], delay_steps={leg: 1}, fill_fraction={leg: D(fraction)})
    result = Engine(data, costs=FREE).run((opening, signals[1]))
    row = next(r for r in result["ledger"] if r["ts"] == 1000 and r["event"] == "frame_end")
    assert row["single_leg_books"] == ["pair"]
    assert row["net_notional"] == 200
    assert not [r for r in result["ledger"] if r["event"] == "funding" and r["symbol"] == "B"]
    assert result["gross_notional"] == 0


def test_fee_slippage_stress_is_two_sided_and_keeps_cash_reconciled():
    _, data, signals = synthetic_inputs()
    first = Engine(data, costs=Costs(D(5), D(5))).run(signals)
    stress = Engine(data, costs=Costs(D(10), D(10))).run(signals)
    assert stress["fees"] > first["fees"] > 0
    assert stress["slippage_diagnostic"] == first["slippage_diagnostic"] * 2
    assert stress["equity"] < first["equity"]
    assert len([r for r in stress["ledger"] if r["event"] == "fill"]) == 4
    assert stress["reconciliation_error"] == first["reconciliation_error"] == 0


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "", None])
def test_nonfinite_inputs_refused(bad):
    with pytest.raises(Rejected):
        number(bad)


@pytest.mark.parametrize("costs", [Costs(D(-1), D(0)), Costs(D(0), D(10000))])
def test_invalid_costs_refused(costs):
    with pytest.raises(Rejected):
        Engine(market([{"A": 100}] * 2), costs=costs).run()


def test_pending_leg_overlap_and_unexecutable_end_signal_refused():
    _, data, signals = synthetic_inputs()
    opening = replace(signals[0], delay_steps={Leg("pair", "B"): 1})
    with pytest.raises(Rejected) as error:
        Engine(data).run((opening, replace(signals[1], known_at=1000)))
    assert error.value.code == "overlapping_signal"
    with pytest.raises(Rejected) as error:
        Engine(data).run([replace(signals[0], known_at=3000)])
    assert error.value.code == "missing_execution"


def test_full_research_gate_does_not_accept_synthetic_data_as_real_evidence():
    _, data, _ = synthetic_inputs()
    with pytest.raises(Rejected) as error:
        require_full_research(data)
    assert error.value.code == "full_research_unavailable"

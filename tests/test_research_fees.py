"""Hand-checkable synthetic fee attribution; no market/cache/provider fixtures."""

import copy
import json
import subprocess
import sys
from dataclasses import replace

import pytest

from research.engine import Costs, Engine, Frame, Leg, Market, Rejected, Signal, number, serializable
from research.fee_scenarios import render, reprice


def fixture_path(targets=("1", "-1", "0"), fraction="1", shared=False):
    times = (0, 1, 3, 5)
    frames = tuple(Frame(t, {"X": number(100 + i)}, {"X": number(100 + i)}) for i, t in enumerate(times))
    market = Market(
        frames,
        times,
        {"X": times},
        {"X": times},
        {(t, "X"): number("0.001") * (-1 if t == 3 else 1) for t in times},
        "synthetic_test",
        "explicit mock funding schedule",
    )
    leg = Leg("first", "X")
    signals = []
    for t, qty in enumerate(targets):
        quantities = {leg: number(qty)}
        if shared:
            quantities[Leg("second", "X")] = -number(qty)
        signals.append(
            Signal(str(t), 2 * t, quantities, fill_fraction={k: number(fraction) for k in quantities})
        )
    result = Engine(market, costs=Costs(number(5), number(5))).run(signals)
    return serializable(result), market


def test_hand_calculated_per_leg_open_reversal_close_fees_and_embedded_slippage():
    accounting, market = fixture_path()
    result = reprice(accounting, market, 5, origin="synthetic_test")
    opening, flip, closing = result["fills"]
    # Buy 1 at 101*(1+.0005); sell 2 at 102*(1-.0005); buy 1 at 103*(1+.0005).
    expected_turnover = number("101.0505") + 2 * number("101.9490") + number("103.0515")
    assert number(result["summary"]["fees"]) == expected_turnover * number("0.0005")
    assert number(opening["opened_or_increased_quantity"]) == 1
    assert number(flip["opened_or_increased_quantity"]) == number(flip["closed_or_reduced_quantity"]) == 1
    assert number(flip["opening_or_increase_fee"]) + number(flip["closing_or_reduction_fee"]) == number(
        flip["fee"]
    )
    assert number(closing["closed_or_reduced_quantity"]) == 1
    summary = result["summary"]
    assert number(summary["slippage_diagnostic_not_subtracted_again"]) == number("0.204")
    assert number(summary["reference_price_trading_pnl"]) == 0
    assert number(summary["executed_price_trading_pnl_including_slippage"]) == number("-0.204")
    assert number(summary["final_equity"]) == number(accounting["equity"])
    # At t=3 the prior long receives negative-rate funding; at t=5 the short receives positive-rate funding.
    assert [number(r["cash_delta"]) for r in result["funding"]] == [number("0.102"), number("0.103")]


def test_opposite_books_same_symbol_do_not_net_transaction_cost_and_funding_signs():
    accounting, market = fixture_path(targets=("1", "1", "0"), shared=True)
    result = reprice(accounting, market, 2, origin="synthetic_test")
    assert len(result["fills"]) == 4  # zero-delta rebalance does not charge.
    notional = sum(number(r["absolute_fill_notional"]) for r in result["fills"])
    assert notional > 400
    assert number(result["summary"]["fees"]) == notional * number("0.0002")
    assert {number(r["cash_delta"]) > 0 for r in result["funding"]} == {True, False}
    assert number(result["summary"]["synthetic_net_funding"]) == 0
    assert all(number(r["fee"]) > 0 for r in result["fills"])


@pytest.mark.parametrize("fraction", ["0", "0.5"])
def test_only_actual_partial_fills_charged_and_unfilled_zero_turnover(fraction):
    accounting, market = fixture_path(targets=("-1",), fraction=fraction)
    result = reprice(accounting, market, 5, origin="synthetic_test")
    fills = result["fills"]
    if fraction == "0":
        assert fills == []
        assert number(result["summary"]["fees"]) == 0
        assert result["summary"]["fee_break_even_bps_given_embedded_slippage"] is None
    else:
        assert len(fills) == 1
        assert number(fills[0]["signed_filled_base_quantity"]) == number("-0.5")
        assert number(fills[0]["fee"]) == number("0.5") * number("100.9495") * number("0.0005")
        assert result["summary"]["terminal_open_legs"] == 1
        assert result["summary"]["hypothetical_terminal_close_fee"] is None


@pytest.mark.parametrize("origin", [None, "okx_m1_snapshot", "real_market"])
def test_real_or_unspecified_origin_rejected_before_validating_or_filling(origin):
    accounting, market = fixture_path()
    with pytest.raises(Rejected) as error:
        reprice(accounting, market, 5, origin=origin)
    assert error.value.code == "real_fee_research_disabled"


@pytest.mark.parametrize("missing", ["rate", "rate_none", "mark", "funding_row", "funding_total"])
def test_missing_funding_or_marks_never_zero_filled(missing):
    accounting, market = fixture_path()
    rates = dict(market.realized_rates)
    if missing == "rate":
        rates.pop((3, "X"))
    if missing == "rate_none":
        rates[3, "X"] = None
    market = replace(market, realized_rates=rates)
    if missing == "mark":
        market = replace(
            market, frames=tuple(replace(f, marks={}) if f.ts == 3 else f for f in market.frames)
        )
    if missing == "funding_row":
        accounting["ledger"] = [r for r in accounting["ledger"] if r["event"] != "funding"]
        for i, row in enumerate(accounting["ledger"]):
            row["seq"] = i
    if missing == "funding_total":
        accounting["funding_cashflow"] = None
    with pytest.raises(Rejected):
        reprice(accounting, market, 5, origin="synthetic_test")


def test_report_all_frozen_cases_reconcile_and_do_not_present_real_metrics():
    output = render()
    payload = json.loads(output["costs.json"])
    real = payload["real_results"]
    assert real["status"] == "unavailable"
    assert all(
        real[k] is None for k in ["funding", "net_result", "trading_pnl", "annualized_return", "sharpe"]
    )
    assert len(payload["cases"]) == 12
    assert "仅合成" in output["REPORT.md"]
    for case in payload["cases"].values():
        scenarios = case["scenarios"]
        taker = scenarios["taker_main"]["summary"]
        maker = scenarios["maker_optimistic"]["summary"]
        assert scenarios["taker_main"]["funding"] == scenarios["maker_optimistic"]["funding"]
        for content in scenarios.values():
            s = content["summary"]
            expected = (
                number(s["reference_price_trading_pnl"])
                - number(s["slippage_diagnostic_not_subtracted_again"])
                + number(s["synthetic_net_funding"])
                - number(s["fees"])
            )
            assert abs(expected - number(s["synthetic_net_result"])) < number("1e-20")
        assert abs(number(maker["fees"]) * 5 - number(taker["fees"]) * 2) < number("1e-20")
        assert abs(
            number(maker["synthetic_net_result"])
            - number(taker["synthetic_net_result"])
            - (number(taker["fees"]) - number(maker["fees"]))
        ) < number("1e-20")


def test_fee_overlay_does_not_mutate_source_and_reports_fixed_path_risk():
    accounting, market = fixture_path(targets=("20",))
    before = copy.deepcopy(accounting)
    cheap = reprice(accounting, market, 0, origin="synthetic_test")
    costly = reprice(accounting, market, 9000, origin="synthetic_test")
    assert accounting == before
    assert (
        costly["summary"]["risk_breach_event_count_on_fixed_path"]
        > cheap["summary"]["risk_breach_event_count_on_fixed_path"]
    )
    assert [r["signed_filled_base_quantity"] for r in cheap["fills"]] == [
        r["signed_filled_base_quantity"] for r in costly["fills"]
    ]


def test_rebalance_increases_and_reductions_are_both_charged():
    accounting, market = fixture_path(targets=("1", "2", "0.5"))
    result = reprice(accounting, market, 5, origin="synthetic_test")
    first, increase, reduction = result["fills"]
    assert [number(r["signed_filled_base_quantity"]) for r in result["fills"]] == [
        number(1),
        number(1),
        number("-1.5"),
    ]
    assert number(increase["opened_or_increased_quantity"]) == 1
    assert number(reduction["closed_or_reduced_quantity"]) == number("1.5")
    assert number(result["summary"]["fees"]) == sum(number(r["fee"]) for r in [first, increase, reduction])


def test_signed_break_even_rate_reconciles_and_negative_budget_is_not_clamped():
    accounting, market = fixture_path()
    rate = reprice(accounting, market, 0, origin="synthetic_test")["summary"][
        "fee_break_even_bps_given_embedded_slippage"
    ]
    at_break_even = reprice(accounting, market, rate, origin="synthetic_test")
    assert abs(number(at_break_even["summary"]["synthetic_net_result"])) < number("1e-20")
    accounting, market = fixture_path(targets=("-1",))
    losing = reprice(accounting, market, 0, origin="synthetic_test")["summary"]
    assert number(losing["fee_break_even_bps_given_embedded_slippage"]) < 0
    assert number(losing["synthetic_net_result"]) < 0


@pytest.mark.parametrize(
    "event,counter",
    [("socket.getaddrinfo", "blocked_network_resolutions"), ("socket.connect", "blocked_network_connects")],
)
def test_fee_CLI_mock_guard_failure_reports_actual_count(event, counter):
    script = r"""
import sys
from research import fee_scenarios as module
calls=[]
event=sys.argv[1]
def mocked(action):
    sys.audit(event, "synthetic-only mock")
    calls.append("must never resolve or connect")
module.perform=mocked
sys.argv=["fee-mock","verify"]
try:
    module.main()
except SystemExit as error:
    assert error.code==3
assert calls==[]
"""
    p = subprocess.run([sys.executable, "-c", script, event], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    payload = json.loads(p.stderr.splitlines()[-1])
    assert payload["synthetic_guard"][counter] == 1
    assert payload["guard_scope"]


def test_real_CLI_refuses_without_attempting_any_market_window():
    p = subprocess.run(
        [sys.executable, "-m", "research.fee_scenarios", "real"], capture_output=True, text=True
    )
    assert p.returncode == 2
    assert "real_fee_research_disabled" in p.stderr

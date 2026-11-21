"""Fixed synthetic paths used to verify execution and accounting protections."""

from datetime import date

from .data import FX, Bar, Market
from .engine import simulate
from .protocol import ASSET_CAP, DAY, INITIAL, SYMBOLS, TARGET, TOTAL_CAP, at, days

SCENARIOS = (
    "correlated_five_day_fall",
    "overnight_gap",
    "missing_three_windows",
    "high_cost",
    "depeg_seven_days",
    "combined",
)


def run_stress():
    output = {}
    start = date(2024, 1, 1)
    end = start + 10 * DAY
    for name in SCENARIOS:
        market = Market(listed=frozenset(SYMBOLS))
        missing = name in {"missing_three_windows", "combined"}
        high_cost = name in {"high_cost", "combined"}
        depeg = name in {"depeg_seven_days", "combined"}
        for offset, day in enumerate(days(start - 60 * DAY, start)):
            # In the no-open and fee-only cases the sell instruction is already actionable
            # at the first check. Other scenarios let the closing signal react with its normal lag.
            prior = (105.0 if offset < 59 else 100.0) if (missing or high_cost) else 94.1 + offset / 10
            for symbol in SYMBOLS:
                market.daily[symbol][day] = Bar(prior, 50_000_000, at(day + DAY))
            market.daily[FX][day] = Bar(1.0, 1.0, at(day + DAY))

        truth = {}
        for offset, day in enumerate(days(start, end)):
            price = 100.0
            if name == "correlated_five_day_fall":
                price = 100 - 4 * min(offset + 1, 5)
            elif missing or name == "overnight_gap":
                price = 60.0
            fx = 0.90 if depeg and offset < 7 else 1.0
            truth[day.isoformat()] = (price, fx)
            for symbol in SYMBOLS:
                market.daily[symbol][day] = Bar(price, 50_000_000, at(day + DAY))
                if not missing or offset >= 3:
                    market.opens[symbol][day] = price
            market.daily[FX][day] = Bar(fx, 1.0, at(day + DAY))

        positions = dict.fromkeys(SYMBOLS, INITIAL * TARGET / 100)
        ledger, _ = simulate(market, start, end, 60 if high_cost else 20, initial_positions=positions)
        previous_cash = INITIAL * (1 - len(SYMBOLS) * TARGET)
        previous_positions = positions
        oracle_nav = [INITIAL]
        checks = {"no_phantom_fills": True, "no_over_limit_additions": True, "nonnegative_cash": True}
        for row in ledger:
            price, fx = truth[row["day"]]
            row["synthetic_truth_nav_before_fills_usd"] = (
                previous_cash + sum(previous_positions.values()) * price
            ) * fx
            row["synthetic_truth_nav_after_fills_usd"] = (
                row["cash_usdt"] + sum(row["quantity"].values()) * price
            ) * fx
            oracle_nav.extend(
                (row["synthetic_truth_nav_before_fills_usd"], row["synthetic_truth_nav_after_fills_usd"])
            )
            previous_cash, previous_positions = row["cash_usdt"], row["quantity"]

            for fill in row["fills"]:
                checks["no_phantom_fills"] &= fill["base_price"] == row["execution_open"][fill["symbol"]]
                if fill["side"] == "buy":
                    checks["no_over_limit_additions"] &= (
                        row["post_total_weight"] <= TOTAL_CAP + 1e-10
                        and max(row["post_weights"].values()) <= ASSET_CAP + 1e-10
                    )
            checks["nonnegative_cash"] &= row["cash_usdt"] >= 0

        peak = INITIAL
        worst = 0.0
        for value in oracle_nav:
            peak = max(peak, value)
            worst = max(worst, 1 - value / peak)
        first_exit = next(
            (row for row in ledger if any(fill["side"] == "sell" for fill in row["fills"])), None
        )
        output[name] = {
            "data_class": "synthetic",
            "cost_bp": 60 if high_cost else 20,
            "worst_truth_drawdown": worst,
            "worst_truth_pre_fill_loss": 1 - min(oracle_nav[1::2]) / INITIAL,
            "worst_truth_post_fill_loss": 1 - min(oracle_nav[2::2]) / INITIAL,
            "ending_truth_usd_return": oracle_nav[-1] / INITIAL - 1,
            "max_post_exposure_measured": max(
                row["post_total_weight"] for row in ledger if row["exposure_measured"]
            ),
            "rejected_sell_attempts": sum(
                item["side"] == "sell" for row in ledger for item in row["rejected"]
            ),
            "max_exit_delay_days": max(
                (item["days"] for row in ledger for item in row["exit_completions"]), default=0
            ),
            "first_exit": {
                "day": first_exit["day"],
                "pre_fill_usd_return": first_exit["synthetic_truth_nav_before_fills_usd"] / INITIAL - 1,
                "post_fill_usd_return": first_exit["synthetic_truth_nav_after_fills_usd"] / INITIAL - 1,
            }
            if first_exit
            else None,
            "protections": checks,
            "ledger": ledger,
        }
    return output

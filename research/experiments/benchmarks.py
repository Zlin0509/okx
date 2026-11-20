"""PIT spot benchmark plans and cash-funded accounting on synthetic daily inputs."""

from dataclasses import dataclass
from decimal import localcontext

from ..engine import BPS, ONE, ZERO, Rejected, number
from ..execution_audit import canonical, sha
from ..portfolio import DAY
from .contract import protocol
from .evaluation_inputs import ORIGIN, DailyReturns, daily_grid, unavailable


@dataclass(frozen=True)
class Tradability:
    known_at: int
    effective_at: int
    listed_at: int | None
    continuous_since: int | None
    tradable: bool
    verified: bool


@dataclass(frozen=True)
class Turnover:
    available_at: int
    confirmed: bool
    volCcyQuote: object


@dataclass(frozen=True)
class SpotPrice:
    price: object
    available_at: int


def _as_of(history, ts):
    known = [m for m in history if m.known_at <= ts and m.effective_at <= ts]
    if known:
        latest = max(known, key=lambda m: (m.effective_at, m.known_at))
        tied = [m for m in known if (m.effective_at, m.known_at) == (latest.effective_at, latest.known_at)]
        if latest.verified is True and all(m == latest for m in tied):
            return latest
    return None


def _rank(ts, perps, spot, turnover):
    rows = []
    for perp in sorted(perps):
        if not perp.endswith("-USDT-SWAP"):
            raise Rejected("invalid_perpetual_universe", perp)
        match = perp.removesuffix("-SWAP")
        contract = _as_of(perps[perp], ts)
        if contract is None or contract.listed_at is None or contract.continuous_since is None:
            raise Rejected("historical_perpetual_metadata_unknown", perp)
        reason, mean = None, None
        if contract.tradable is not True:
            reason = "perpetual_not_tradable"
        elif ts - max(contract.listed_at, contract.continuous_since) < 180 * DAY:
            reason = "continuous_age_under_180_days"
        else:
            listing = _as_of(spot.get(match, ()), ts)
            if listing is None or (listing.tradable and listing.listed_at is None):
                raise Rejected("historical_spot_metadata_unknown", match)
            if listing.tradable is not True or listing.listed_at > ts:
                reason = "matching_spot_not_tradable"
            else:
                values = []
                for lag in range(1, 31):
                    close = ts - (lag - 1) * DAY - 1
                    volume = turnover.get(perp, {}).get(close)
                    if (
                        volume is None
                        or volume.confirmed is not True
                        or not close <= volume.available_at <= ts
                    ):
                        raise Rejected("missing_confirmed_lagged_turnover", perp)
                    value = number(volume.volCcyQuote)
                    if value < 0:
                        raise Rejected("invalid_lagged_turnover", perp)
                    values.append(value)
                mean = sum(values, ZERO) / 30
        rows.append({"perp": perp, "spot": match, "mean": mean, "excluded": reason})
    ordered = sorted((r for r in rows if r["excluded"] is None), key=lambda r: (-r["mean"], r["perp"]))
    if len(ordered) < 6:
        raise Rejected("fewer_than_six_baseline_members", str(ts))
    selected = ordered[:8]
    for row in ordered[8:]:
        row["excluded"] = "below_top_eight"
    return {
        "known_at": ts,
        "members": [r["spot"] for r in selected],
        "registry": [dict(r, mean=str(r["mean"]) if r["mean"] is not None else None) for r in rows],
    }


def _price(source, symbol, ts):
    price = source.get((ts, symbol))
    if price is None or price.available_at != ts or number(price.price) <= 0:
        raise Rejected("missing_or_unavailable_spot_price", f"{symbol} at {ts}")
    return number(price.price)


def _book(days, plan, spot, opens, closes, scenario, capital):
    settings = protocol()["cost_scenarios"][scenario]
    fee, slip = number(settings["fee_bps"]) / BPS, number(settings["slippage_bps"]) / BPS
    cash = previous = number(capital)
    held, trades, values, nav = {}, [], [], []

    def traded(symbol, delta, reference, ts, known_at):
        nonlocal cash
        execution = reference * (ONE + slip if delta > 0 else ONE - slip)
        notional = abs(delta) * execution
        charge = notional * fee
        cash -= delta * execution + charge
        held[symbol] = held.get(symbol, ZERO) + delta
        trades.append(
            {
                "symbol": symbol,
                "quantity": str(delta),
                "fee": str(charge),
                "notional": str(notional),
                "execution_price": str(execution),
                "slippage_diagnostic": str(abs(delta) * abs(execution - reference)),
                "executed_at": ts,
                "known_at": known_at,
            }
        )

    for day in days:
        instruction = plan.get(day)
        if instruction is not None:
            known_at, members = instruction
            active = {symbol for symbol, quantity in held.items() if quantity}
            marks = {}
            for symbol in sorted(active | set(members)):
                for t in (known_at, day):
                    listing = _as_of(spot.get(symbol, ()), t)
                    if (
                        listing is None
                        or listing.tradable is not True
                        or listing.listed_at is None
                        or listing.listed_at > t
                    ):
                        raise Rejected("spot_not_verified_at_decision_and_fill", f"{symbol} at {t}")
                marks[symbol] = _price(opens, symbol, day)
            equity = cash + sum((held[symbol] * marks[symbol] for symbol in active), ZERO)
            # Equal pre-slippage reference notional for the retained and purchased legs after costs.
            # Solve for the common reference notional that leaves zero cash. The cash
            # balance is monotone in the target because every incremental buy consumes cash
            # and every incremental reduction forgoes sale proceeds.
            if members:
                buy_factor = (ONE + slip) * (ONE + fee)
                sell_factor = (ONE - slip) * (ONE - fee)
                current = {s: held.get(s, ZERO) * marks[s] for s in marks}
                available = cash + sum(
                    (v * sell_factor for s, v in current.items() if s not in members), ZERO
                )
                low, high = ZERO, equity / len(members)
                for _ in range(120):
                    mid = (low + high) / 2
                    balance = available + sum(
                        (current[s] - mid) * (sell_factor if current[s] >= mid else buy_factor)
                        for s in members
                    )
                    if balance >= 0:
                        low = mid
                    else:
                        high = mid
                targets = {s: low / marks[s] for s in members}
            else:
                targets = {}
            deltas = {s: targets.get(s, ZERO) - held.get(s, ZERO) for s in marks}
            for symbol in sorted(deltas, key=lambda s: (deltas[s] > 0, s)):
                delta = deltas[symbol]
                if delta > 0:
                    buy_at = marks[symbol] * (ONE + slip)
                    spendable = cash / (buy_at * (ONE + fee))
                    delta = min(spendable, delta)
                    # Decimal division can round up at the last representable digit.
                    while delta > 0 and delta * buy_at + (delta * buy_at) * fee > cash:
                        delta = delta.next_minus()
                    delta = max(delta, ZERO)
                if delta:
                    traded(symbol, delta, marks[symbol], day, known_at)
        equity = cash + sum(
            (q * _price(closes, s, day + DAY - 1) for s, q in sorted(held.items()) if q), ZERO
        )
        if equity <= 0:
            raise Rejected("invalid_spot_equity", str(day))
        values.append(float(equity / previous - ONE))
        nav.append(str(equity))
        previous = equity
    total = sum((number(t["notional"]) for t in trades), ZERO)
    return {
        "returns": DailyReturns(ORIGIN, scenario, capital, days, tuple(values)),
        "nav": nav,
        "fills": trades,
        "terminal_cash": str(cash),
        "terminal_quantities": {s: str(q) for s, q in sorted(held.items()) if q},
        "absolute_turnover_over_initial_capital": str(total / capital),
        "funding_cashflow": "0",
    }


def spot_benchmarks(
    *,
    origin,
    scenario,
    days,
    universe_verified,
    perps,
    spot,
    turnover,
    opens,
    closes,
    btc_entry_known_at,
    liquidation_known_at=None,
):
    """One common calendar, independent BTC and equal-weight cash ledgers, no inferred fills."""
    if origin != ORIGIN:
        return unavailable("real_benchmark_execution_disabled")
    if universe_verified is not True:
        return unavailable("historical_global_universe_unknown")
    try:
        days = daily_grid(days)
        p = protocol()
        if scenario not in p["cost_scenarios"]:
            raise Rejected("unregistered_cost_scenario", scenario)
        if type(btc_entry_known_at) is not int:
            raise Rejected("missing_btc_entry_decision", "Explicit prior BTC decision required")
        entry = (btc_entry_known_at // DAY + 1) * DAY
        if entry not in days:
            raise Rejected("btc_entry_outside_calendar", str(entry))
        btc = {entry: (btc_entry_known_at, ("BTC-USDT",))}
        exit_day = None
        if liquidation_known_at is not None:
            if type(liquidation_known_at) is not int:
                raise Rejected("invalid_terminal_execution", "Explicit exit decision required")
            exit_day = (liquidation_known_at // DAY + 1) * DAY
            if exit_day not in days or liquidation_known_at < entry:
                raise Rejected("missing_terminal_execution", str(exit_day))
            btc[exit_day] = (liquidation_known_at, ())
        # Register every Monday selection before touching any execution or valuation price.
        weekday = p["decision_calendar"]["epoch_day_mod7"]
        with localcontext() as ctx:
            ctx.prec = 34
            registry = [
                _rank(t, perps, spot, turnover)
                for t in range(days[0] - DAY, days[-1], DAY)
                if (t // DAY) % 7 == weekday and (exit_day is None or t < liquidation_known_at)
            ]
            if not registry:
                raise Rejected(
                    "missing_weekly_benchmark_decisions", "At least one frozen weekly basket needed"
                )
            equal = {r["known_at"] + DAY: (r["known_at"], tuple(r["members"])) for r in registry}
            if exit_day is not None:
                equal[exit_day] = (liquidation_known_at, ())
            capital = p["risk"]["initial_equity"]
            return {
                "status": "synthetic_diagnostic",
                "origin": ORIGIN,
                "performance_evidence": False,
                "registry": registry,
                "registry_sha256": sha(canonical(registry)),
                "btc": _book(days, btc, spot, opens, closes, scenario, capital),
                "equal_weight": _book(days, equal, spot, opens, closes, scenario, capital),
            }
    except Rejected as exc:
        return unavailable(exc.code)
    except (AttributeError, TypeError):
        return unavailable("missing_spot_benchmark_input")

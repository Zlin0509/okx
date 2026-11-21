"""Daily, offline long-only simulator for the frozen ZLIN-21 protocol."""

import math
import statistics
from dataclasses import dataclass, field

from .data import FX, Market
from .protocol import (
    ASSET_CAP,
    CHECK_HOUR,
    DAY,
    INITIAL,
    LIQUIDITY_DAYS,
    MAX_DRAWDOWN,
    MAX_TURNOVER,
    MAX_VOLATILITY,
    MIN_ROLLING_30,
    MIN_TURNOVER,
    SYMBOLS,
    TARGET,
    TOTAL_CAP,
    TREND_DAYS,
    at,
    days,
)

EPSILON = 1e-10


@dataclass(frozen=True)
class Cost:
    total_bp: int

    @property
    def fee(self):
        return 0.001

    @property
    def adjustment(self):
        # Keep the 5 bp half spread and 10 bp fee; the sensitivity increment is slippage.
        return (self.total_bp - 10) / 10_000

    @property
    def buy_multiplier(self):
        return (1 + self.adjustment) * (1 + self.fee)


@dataclass
class Portfolio:
    cash: float = INITIAL
    quantity: dict[str, float] = field(default_factory=lambda: dict.fromkeys(SYMBOLS, 0.0))

    def equity(self, marks):
        return self.cash + sum(self.quantity[symbol] * marks[symbol] for symbol in marks)

    def weights(self, marks):
        equity = self.equity(marks)
        return {symbol: self.quantity[symbol] * marks.get(symbol, 0.0) / equity for symbol in SYMBOLS}

    def trade(self, symbol, side, quantity, price, cost):
        buy = side == "buy"
        fill = price * (1 + cost.adjustment if buy else 1 - cost.adjustment)
        if buy:
            quantity = min(quantity, self.cash / cost.buy_multiplier / price)
        else:
            quantity = min(quantity, self.quantity[symbol])
        if quantity * price <= EPSILON:
            return None
        notional = quantity * fill
        fee = notional * cost.fee
        if buy:
            self.cash = max(0.0, self.cash - notional - fee)
            self.quantity[symbol] += quantity
        else:
            self.cash += notional - fee
            self.quantity[symbol] -= quantity
        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "base_price": price,
            "fill_price": fill,
            "notional_usdt": notional,
            "fee_usdt": fee,
            "execution_drag_usdt": abs(fill - price) * quantity + fee,
        }


def decision(market, day):
    check = at(day, CHECK_HOUR)
    active = {}
    diagnostics = {}
    stale = []
    for symbol in SYMBOLS:
        history = market.recent(symbol, day, TREND_DAYS, check)
        recent = history[-LIQUIDITY_DAYS:]
        latest = history[-1]
        trend_avg = statistics.fmean(bar.close for bar in history) if all(history) else None
        volume_median = statistics.median(bar.turnover for bar in recent) if all(recent) else None
        if trend_avg is None:
            stale.append(symbol)
        status = market.known_interruption(symbol, day, check)
        eligible = (
            symbol in market.listed
            and not status
            and volume_median is not None
            and (volume_median >= MIN_TURNOVER)
        )
        trend = trend_avg is not None and latest.close > trend_avg
        diagnostics[symbol] = {
            "latest_close": latest.close if latest else None,
            "sma60": trend_avg,
            "median_quote30": volume_median,
            "listed": symbol in market.listed,
            "known_interruption": status,
            "eligible": eligible,
            "trend": trend,
        }
        active[symbol] = eligible and trend
    if not diagnostics[SYMBOLS[0]]["trend"]:
        active = dict.fromkeys(SYMBOLS, False)
    fx = market.bar(FX, day - DAY, check)
    if not fx:
        stale.append(FX)
    depeg = fx is not None and (fx.close <= 0.98 or fx.close >= 1.02)
    return active, stale, depeg, diagnostics


def _initial_mark(market, symbol, day):
    as_of = at(day, CHECK_HOUR)
    return next(
        (
            bar.close
            for past in sorted(market.daily[symbol], reverse=True)
            if past < day and (bar := market.bar(symbol, past, as_of))
        ),
        None,
    )


def _trim(portfolio, symbol, target, marks, price, cost):
    # Solve for a post-fee target using the observed mark and actual sale proceeds.
    equity = portfolio.equity(marks)
    drag = 1 - (1 - cost.adjustment) * (1 - cost.fee)
    excess = portfolio.quantity[symbol] * price - target * equity
    if excess <= EPSILON:
        return None
    return portfolio.trade(symbol, "sell", excess / (price * (1 - target * drag)), price, cost)


def _cap_excess(weights):
    portfolio_breach = sum(weights.values()) > TOTAL_CAP + EPSILON
    return [
        symbol
        for symbol, weight in weights.items()
        if weight > ASSET_CAP + EPSILON or (portfolio_breach and weight > TARGET)
    ]


def _execute_exits(portfolio, pending, prices, marks, cost, day, fills, rejected, completed):
    for symbol, (target, since) in list(pending.items()):
        price = prices[symbol]
        if price is None:
            rejected.append(
                {
                    "symbol": symbol,
                    "side": "sell",
                    "reason": "window_unavailable",
                    "first_intent": since.isoformat(),
                }
            )
            continue
        fill = _trim(portfolio, symbol, target, marks, price, cost)
        if fill:
            fills.append(fill)
            completed.append({"symbol": symbol, "days": (day - since).days, "target": target})
        del pending[symbol]


def _row(portfolio, day, fx, end_fx, opening, marks, prices, pre, fills, details):
    post = portfolio.equity(opening)
    end = portfolio.equity(marks)
    weights = portfolio.weights(opening)
    measured = all(prices.values())
    breached = any(weight > ASSET_CAP + EPSILON for weight in weights.values()) or (
        sum(weights.values()) > TOTAL_CAP + EPSILON
    )
    return {
        "day": day.isoformat(),
        "cash_usdt": portfolio.cash,
        "quantity": dict(portfolio.quantity),
        "execution_open": prices,
        "post_weights": weights,
        "post_total_weight": sum(weights.values()),
        "exposure_measured": measured,
        "post_exposure_breach": breached,
        "nav_pre_usdt": pre,
        "nav_pre_usd": pre * fx if fx is not None else None,
        "nav_post_usd": post * fx if fx is not None else None,
        "nav_end_usdt": end,
        "nav_end_usd": end * end_fx if end_fx is not None else None,
        "turnover": sum(fill["notional_usdt"] for fill in fills) / pre,
        "fills": fills,
        **details,
    }


def simulate(market: Market, start, end, total_bp, *, initial_positions=None, passive=False):
    portfolio = Portfolio()
    if initial_positions:
        portfolio = Portfolio(INITIAL * (1 - 2 * TARGET), dict(initial_positions))
    cost = Cost(total_bp)
    mark = {symbol: price for symbol in SYMBOLS if (price := _initial_mark(market, symbol, start))}
    fx_last = _initial_mark(market, FX, start)
    initial_usd = INITIAL * fx_last if fx_last is not None else None
    pending = {}
    invested = False
    ledger = []
    for day in days(start, end):
        active, stale, depeg, signals = decision(market, day)
        check_marks = {
            symbol: candle.close
            for symbol in SYMBOLS
            if (candle := market.bar(symbol, day - DAY, at(day, CHECK_HOUR)))
        }
        mark.update(check_marks)
        previous_fx = market.bar(FX, day - DAY, at(day, CHECK_HOUR))
        if previous_fx:
            fx_last = previous_fx.close
        current_fx = fx_last
        prices = {symbol: market.execution_price(symbol, day) for symbol in SYMBOLS}
        opening = mark | {symbol: price for symbol, price in prices.items() if price is not None}
        pre = portfolio.equity(opening)
        fills = []
        rejected = []
        delayed = []
        attempted_adds = []
        risk_trim = []
        unavailable = [symbol for symbol, price in prices.items() if price is None]
        blocked = [*stale, *[f"{symbol}:execution" for symbol in unavailable]]
        if depeg:
            blocked.append("usdt_depeg")
        check_adds = [
            symbol
            for symbol in SYMBOLS
            if active[symbol] and day.weekday() == 0 and not stale and not depeg and symbol not in pending
        ]
        check_exits = [symbol for symbol in SYMBOLS if portfolio.quantity[symbol] > 0 and not active[symbol]]
        check_interruptions = [symbol for symbol in SYMBOLS if signals[symbol]["known_interruption"]]
        if passive:
            if not invested and not unavailable and market.listed == frozenset(SYMBOLS):
                budget = portfolio.cash / len(SYMBOLS)
                for symbol, price in prices.items():
                    fills.append(
                        portfolio.trade(symbol, "buy", budget / cost.buy_multiplier / price, price, cost)
                    )
                invested = True
            elif not invested and unavailable:
                rejected.extend(
                    {"symbol": symbol, "side": "buy", "reason": "window_unavailable"}
                    for symbol in unavailable
                )
        else:
            risk_trim = _cap_excess(portfolio.weights(opening))
            for symbol in risk_trim:
                pending.setdefault(symbol, (TARGET, day))
            for symbol in check_exits:
                pending[symbol] = (0.0, pending.get(symbol, (0.0, day))[1])
            had_pending = bool(pending)
            _execute_exits(portfolio, pending, prices, opening, cost, day, fills, rejected, delayed)
            if fills:
                # A fee on the first sale can put an unaffected holding above its cap.
                fee_induced = {
                    symbol: (TARGET, day)
                    for symbol in _cap_excess(portfolio.weights(opening))
                    if symbol not in pending
                }
                risk_trim.extend(symbol for symbol in fee_induced if symbol not in risk_trim)
                _execute_exits(portfolio, fee_induced, prices, opening, cost, day, fills, rejected, delayed)
                pending.update(fee_induced)
            if had_pending:
                blocked.append("exit_or_risk_trim")
            attempted_adds = [
                symbol for symbol in check_adds if not blocked and portfolio.weights(opening)[symbol] < TARGET
            ]
            if attempted_adds:
                # Reserve execution drag and enforce the limits against post-cost NAV.
                drag = cost.buy_multiplier - 1
                target_total = TARGET * len(SYMBOLS)
                values = {symbol: portfolio.quantity[symbol] * prices[symbol] for symbol in SYMBOLS}
                post_cost_floor = pre / (1 + drag * target_total)
                desired = {
                    symbol: max(0, TARGET * post_cost_floor - values[symbol]) for symbol in attempted_adds
                }
                capacity = min(
                    (target_total * pre - sum(values.values())) / (1 + target_total * drag),
                    (pre - max(values.values()) / ASSET_CAP) / drag,
                )
                requested = sum(desired.values())
                scale = min(1.0, max(0.0, capacity) / requested) if requested else 0.0
                for symbol, notional in desired.items():
                    price = prices[symbol]
                    fill = portfolio.trade(symbol, "buy", scale * notional / price, price, cost)
                    if fill:
                        fills.append(fill)
            if unavailable:
                rejected.extend(
                    {"symbol": symbol, "side": "buy", "reason": "key_window_price_missing"}
                    for symbol in check_adds
                    if not stale and not depeg and not had_pending
                )

        mark = opening.copy()
        stale_eod = []
        for symbol in SYMBOLS:
            bar = market.bar(symbol, day, at(day + DAY))
            if bar:
                mark[symbol] = bar.close
            else:
                stale_eod.append(symbol)
        end_fx = market.bar(FX, day, at(day + DAY))
        if end_fx:
            fx_last = end_fx.close
        else:
            stale_eod.append(FX)
        ledger.append(
            _row(
                portfolio,
                day,
                current_fx,
                fx_last,
                opening,
                mark,
                prices,
                pre,
                fills,
                {
                    "fx_at_check": current_fx,
                    "fx_at_close": fx_last,
                    "signals": signals,
                    "check_add_candidates": check_adds,
                    "check_exit_intents": check_exits,
                    "known_market_interruptions": check_interruptions,
                    "risk_trims": risk_trim,
                    "attempted_adds": attempted_adds,
                    "add_blocks": blocked,
                    "stale_eod": stale_eod,
                    "rejected": rejected,
                    "exit_completions": delayed,
                    "pending_exits": {
                        symbol: {"target": target, "since": since.isoformat(), "days": (day - since).days}
                        for symbol, (target, since) in pending.items()
                    },
                },
            )
        )
    return ledger, initial_usd


def series_metrics(nav):
    if not nav or any(value is None for value in nav):
        return dict.fromkeys(("return", "max_drawdown", "worst_rolling_30d", "annualized_volatility"))
    peak = nav[0]
    drawdown = 0.0
    for value in nav:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    daily = [right / left - 1 for left, right in zip(nav, nav[1:], strict=False)]
    return {
        "return": nav[-1] / nav[0] - 1,
        "max_drawdown": drawdown,
        "worst_rolling_30d": min(
            (nav[index] / nav[index - 30] - 1 for index in range(30, len(nav))), default=None
        ),
        "annualized_volatility": statistics.stdev(daily) * math.sqrt(365) if len(daily) > 1 else None,
    }


def metrics(ledger, initial_usd, *, assess=False):
    usd = series_metrics([initial_usd, *(row["nav_end_usd"] for row in ledger)])
    annualized_turnover = sum(row["turnover"] for row in ledger) * 365 / len(ledger)
    fresh = [row for row in ledger if row["exposure_measured"]]
    checks = {
        "drawdown": usd["max_drawdown"] is not None and usd["max_drawdown"] <= MAX_DRAWDOWN,
        "rolling_30d": usd["worst_rolling_30d"] is not None and usd["worst_rolling_30d"] >= MIN_ROLLING_30,
        "volatility": usd["annualized_volatility"] is not None
        and usd["annualized_volatility"] <= MAX_VOLATILITY,
        "exposure": bool(fresh) and not any(row["post_exposure_breach"] for row in fresh),
        "turnover": annualized_turnover <= MAX_TURNOVER,
    }
    exit_delays = [item["days"] for row in ledger for item in row["exit_completions"]]
    return {
        "usd": usd,
        "usdt_return": ledger[-1]["nav_end_usdt"] / INITIAL - 1,
        "annualized_two_way_turnover": annualized_turnover,
        "max_post_asset_exposure_measured": max(
            (max(row["post_weights"].values()) for row in fresh), default=None
        ),
        "max_post_exposure_by_asset_measured": {
            symbol: max((row["post_weights"][symbol] for row in fresh), default=None) for symbol in SYMBOLS
        },
        "max_post_total_exposure_measured": max((row["post_total_weight"] for row in fresh), default=None),
        "post_exposure_breaches_measured": [row["day"] for row in fresh if row["post_exposure_breach"]],
        "post_exposure_breaches_stale": [
            row["day"] for row in ledger if not row["exposure_measured"] and row["post_exposure_breach"]
        ],
        "stale_execution_days": len(ledger) - len(fresh),
        "stale_valuation_days": sum(bool(row["stale_eod"]) for row in ledger),
        "rejected_trades": sum(len(row["rejected"]) for row in ledger),
        "completed_delayed_exits": sum(delay > 0 for delay in exit_delays),
        "max_completed_exit_delay_days": max(exit_delays, default=0),
        "unresolved_exits": ledger[-1]["pending_exits"],
        "trades": sum(len(row["fills"]) for row in ledger),
        "fees_usdt": sum(fill["fee_usdt"] for row in ledger for fill in row["fills"]),
        "execution_drag_usdt": sum(fill["execution_drag_usdt"] for row in ledger for fill in row["fills"]),
        "numeric_checks": checks if assess else None,
        "acceptance": "insufficient_evidence" if assess else None,
    }

"""Sequential first-trade replay using the frozen v1 decisions and accounting."""

from itertools import groupby

from ..data import FX
from ..engine import Cost, Portfolio, _cap_excess, _row, _trim, buy_allocations, decision
from ..protocol import CHECK_HOUR, DAY, EXEC_HOUR, INITIAL, SYMBOLS, TARGET, at, days
from .calendar import END, EVAL_START
from .evidence import LIVE, TRADE_SOURCE, Evidence, Span, Trade


def _checks(market, day, marks, last_fx):
    check = at(day, CHECK_HOUR)
    active, stale, depeg, diagnostics = decision(market, day)
    for symbol in SYMBOLS:
        if bar := market.bar(symbol, day - DAY, check):
            marks[symbol] = bar.close
    if not market.live_at_check(FX, day):
        stale.append("fx_status")
    bar = market.bar(FX, day - DAY, check)
    return active, stale, depeg, diagnostics, bar.close if bar else last_fx


def _events(market, day):
    return sorted(
        (tick for symbol in SYMBOLS if (tick := market.first_trade(day, symbol))),
        key=lambda tick: (tick.received, tick.timestamp, tick.symbol),
    )


def _event_batches(events):
    # Clocks on the same receipt and venue timestamp can share both marks. Nothing later is consumed.
    for _, group in groupby(events, key=lambda item: (item.received, item.timestamp)):
        yield {tick.symbol: tick for tick in group}


def _fill_metadata(fill, tick):
    return {
        **fill,
        "exchange_at": tick.timestamp.isoformat(),
        "received_at": tick.received.isoformat(),
        "trade_id": tick.trade_id,
        "source": TRADE_SOURCE,
    }


def _register_caps(portfolio, marks, pending, caps, day):
    for symbol in _cap_excess(portfolio.weights(marks)):
        caps.add(symbol)
        pending.setdefault(symbol, (TARGET, day))


def _sell_on_batch(portfolio, marks, batch, pending, cost, day, fills, completions):
    for symbol, item in batch.items():
        if symbol not in pending:
            continue
        target, since = pending.pop(symbol)
        if fill := _trim(portfolio, symbol, target, marks, item.price, cost):
            fills.append(_fill_metadata(fill, item))
            completions.append({"symbol": symbol, "target": target, "days": (day - since).days})


def _additions_allowed(market, day, tick, portfolio, observed):
    # A holding with no price yet in this window makes its current cap unverifiable.
    return all(
        (portfolio.quantity[symbol] == 0 or symbol in observed)
        and market.continuous((day, symbol), tick.received)
        for symbol in SYMBOLS
    ) and market.continuous((day, FX), tick.received)


def simulate(market, total_bp, *, passive=False, start=EVAL_START, end=END, initial=None):
    portfolio = initial if initial is not None else Portfolio()
    cost = Cost(total_bp)
    pending = {}
    marks = {}
    last_fx = None
    initial_usd = None
    passive_filled = set()
    passive_incomplete = False
    ledger = []
    for day in days(start, end):
        active, stale, depeg, signals, last_fx = _checks(market, day, marks, last_fx)
        check_fx = last_fx
        if day == start and check_fx is not None:
            initial_usd = INITIAL * check_fx
        exits = (
            []
            if passive
            else [symbol for symbol in SYMBOLS if portfolio.quantity[symbol] and not active[symbol]]
        )
        for symbol in exits:
            pending[symbol] = (0.0, pending.get(symbol, (0.0, day))[1])
        caps = set()
        if not passive:
            _register_caps(portfolio, marks, pending, caps, day)
        risk_day = bool(pending)
        candidates = [
            symbol for symbol in SYMBOLS if active[symbol] and day.weekday() == 0 and not (stale or depeg)
        ]
        passive_start = passive and not passive_filled and all(market.live_at_check(s, day) for s in SYMBOLS)
        fills, rejected, completions, events = [], [], [], []
        prices = dict.fromkeys(SYMBOLS)
        observed = set()
        pre = portfolio.equity(marks)
        previous_venue_ts = None
        for batch in _event_batches(_events(market, day)):
            tick = next(iter(batch.values()))
            if previous_venue_ts and tick.timestamp < previous_venue_ts:
                # A cross-instrument reversal cannot be attributed to an earlier exchange print.
                rejected.extend(
                    {"symbol": symbol, "reason": "cross_venue_clock_reversal"} for symbol in batch
                )
                continue
            previous_venue_ts = tick.timestamp
            for symbol, item in batch.items():
                prices[symbol] = marks[symbol] = item.price
            if not observed:
                pre = portfolio.equity(marks)
            observed.update(batch)
            if passive_start:
                # Both legs are committed in advance with independent half cash budgets.
                for symbol, item in batch.items():
                    if all(market.continuous((day, s), item.received) for s in SYMBOLS):
                        quantity = (INITIAL / len(SYMBOLS)) / cost.buy_multiplier / item.price
                        if fill := portfolio.trade(symbol, "buy", quantity, item.price, cost):
                            fills.append(_fill_metadata(fill, item))
                            passive_filled.add(symbol)
            if not passive:
                _register_caps(portfolio, marks, pending, caps, day)
                risk_day |= bool(pending)
                _sell_on_batch(portfolio, marks, batch, pending, cost, day, fills, completions)
                _register_caps(portfolio, marks, pending, caps, day)
                # Fee-induced caps can still use a peer print from the exact same event.
                _sell_on_batch(portfolio, marks, batch, pending, cost, day, fills, completions)
                eligible = [symbol for symbol in batch if symbol in candidates]
                if eligible and not risk_day and _additions_allowed(market, day, tick, portfolio, observed):
                    for symbol, amount in buy_allocations(portfolio, marks, eligible, cost).items():
                        item = batch[symbol]
                        if fill := portfolio.trade(symbol, "buy", amount / item.price, item.price, cost):
                            fills.append(_fill_metadata(fill, item))
                elif eligible and not risk_day:
                    rejected.extend(
                        {"symbol": symbol, "side": "buy", "reason": "unverified_current_marks_or_status"}
                        for symbol in eligible
                    )
            measured = all(not portfolio.quantity[symbol] or symbol in observed for symbol in SYMBOLS)
            events.append(
                {
                    "received_at": tick.received.isoformat(),
                    "exchange_at": tick.timestamp.isoformat(),
                    "symbols": sorted(batch),
                    "valuation_measured": measured,
                    "weights": portfolio.weights(marks),
                    "cap_excess": _cap_excess(portfolio.weights(marks)),
                }
            )
        if passive_start and passive_filled and len(passive_filled) != len(SYMBOLS):
            passive_incomplete = True
        if not passive:
            rejected.extend(
                {
                    "symbol": symbol,
                    "side": "sell",
                    "reason": "no_remaining_proven_first_trade",
                    "first_intent": since.isoformat(),
                }
                for symbol, (_, since) in pending.items()
            )
            rejected.extend(
                {"symbol": symbol, "side": "buy", "reason": "no_proven_first_trade"}
                for symbol in candidates
                if symbol not in observed and not risk_day
            )

        opening = marks.copy()
        stale_eod = []
        for symbol in (*SYMBOLS, FX):
            bar = market.bar(symbol, day, at(day + DAY, CHECK_HOUR))
            if not bar:
                stale_eod.append(symbol)
            elif symbol == FX:
                last_fx = bar.close
            else:
                marks[symbol] = bar.close
        ledger.append(
            _row(
                portfolio,
                day,
                check_fx,
                last_fx,
                opening,
                marks,
                prices,
                pre,
                fills,
                {
                    "signals": signals,
                    "check_add_candidates": candidates,
                    "check_exit_intents": exits,
                    "fx_at_check": check_fx,
                    "fx_at_close": last_fx,
                    "checked_stale": stale,
                    "depeg": depeg,
                    "stale_eod": stale_eod,
                    "rejected": rejected,
                    "exit_completions": completions,
                    "execution_events": events,
                    "risk_trims": sorted(caps),
                    "passive_incomplete": passive_incomplete,
                    "pending_exits": {
                        symbol: {"target": target, "since": since.isoformat(), "days": (day - since).days}
                        for symbol, (target, since) in pending.items()
                    },
                },
            )
        )
    return ledger, initial_usd


def synthetic_replay(market, start, end, total_bp, *, initial_positions):
    """Bridge the preregistered synthetic shock paths to the v2 execution clock."""
    proof = Evidence(daily=market.daily, listed=market.listed)
    for day in days(start, end):
        first, last = at(day, EXEC_HOUR), at(day, EXEC_HOUR + 1)
        for symbol, state in LIVE.items():
            key = day, symbol
            proof.checks[key] = state
            proof.states[key] = [Span(first, last, state)]
            if symbol in SYMBOLS and (price := market.opens[symbol].get(day)) is not None:
                proof.feeds[key] = [Span(first, last, symbol)]
                proof.trades[key] = [Trade(symbol, str(day), first, first, price, symbol)]
    portfolio = Portfolio(INITIAL * (1 - len(SYMBOLS) * TARGET), dict(initial_positions))
    return simulate(proof, total_bp, start=start, end=end, initial=portfolio)

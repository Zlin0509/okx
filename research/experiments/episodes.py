"""Count actual flat-to-flat relation-book episodes without altering the source ledger."""

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from types import SimpleNamespace

from ..engine import ZERO, Position, Rejected, fill_position, number
from ..portfolio import DAY, hedge_error
from .contract import protocol
from .evaluation_inputs import ORIGIN, daily_grid, unavailable


@dataclass(frozen=True)
class EntryIntent:
    book: str
    known_at: int
    # Directed, frozen hedge notional vector; zero-beta legs are omitted from qualification.
    notionals: dict[str, object]


@dataclass
class Episode:
    book: str
    start: int
    signal: str
    intent: EntryIntent
    qualified_at: int | None = None
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    fill_parts: list = field(default_factory=list)


def _record(episode, stop, unrealized, eligible_start, eligible_stop):
    overlapping = episode.start < eligible_stop and (stop is None or stop >= eligible_start)
    if not overlapping:
        status = "outside_eligible_interval"
    elif episode.start < eligible_start or (stop is not None and stop >= eligible_stop):
        status = "split_overlap"
    elif stop is None:
        status = "terminal_open"
    elif episode.qualified_at is None:
        status = "closed_unqualified_orphan_or_hedge_error"
    else:
        status = "qualifying_round_trip"
    return {
        "book": episode.book,
        "first_fill": episode.start,
        "entry_signal": episode.signal,
        "flat_at": stop,
        "hedge_established_at": episode.qualified_at,
        "status": status,
        "terminal_open": stop is None,
        "fill_parts": episode.fill_parts,
        "realized_pnl": str(episode.realized),
        "fee": str(episode.fees),
        "funding_cashflow": str(episode.funding),
        "terminal_unrealized_pnl": str(unrealized),
        "net_pnl": str(episode.realized + episode.funding + unrealized - episode.fees),
    }


def _mark(marks, ts, symbol):
    value = marks.get(ts, {}).get(symbol)
    if value is None or number(value) <= 0:
        raise Rejected("missing_episode_mark", f"{symbol} at {ts}")
    return number(value)


def _establish(episode, positions, timestamp, marks, tolerance):
    if episode.qualified_at is not None:
        return
    vector = {s: number(v) for s, v in sorted(episode.intent.notionals.items()) if number(v)}
    if set(vector) != set(positions) or len(vector) < 2:
        return
    if any(v * positions[s].quantity <= 0 for s, v in vector.items()):
        return
    notionals = {s: p.quantity * _mark(marks, timestamp, s) for s, p in positions.items()}
    # Normalize before float conversion to preserve the frozen shape and avoid scale overflow.
    scale = max(abs(v) for v in vector.values())
    gross = sum(map(abs, notionals.values()), ZERO)
    model = SimpleNamespace(symbols=tuple(vector), vector=tuple(float(v / scale) for v in vector.values()))
    if hedge_error(model, {s: float(v / gross) for s, v in notionals.items()}) <= tolerance:
        episode.qualified_at = timestamp


def _tally(ledger, intents, marks, days, scenario):
    start, stop = days[0], days[-1] + DAY
    if (
        not ledger
        or ledger[0]["ts"] > start
        or ledger[-1]["ts"] != stop - 1
        or ledger[-1]["event"] != "frame_end"
    ):
        raise Rejected("incomplete_episode_ledger", "Require pre-boundary history and last daily close")
    if any(p["quantity"] and number(p["quantity"]) for p in ledger[0]["positions"]):
        raise Rejected("unknown_initial_book_positions", "Ledger must start flat")
    settings = protocol()
    if scenario not in settings["cost_scenarios"]:
        raise Rejected("unregistered_cost_scenario", str(scenario))
    closes = {r["ts"] for r in ledger if r["event"] == "frame_end"}
    if any(day + DAY - 1 not in closes for day in days):
        raise Rejected("missing_episode_daily_close", "Ledger must cover every eligible close")
    tolerance = settings["risk"]["hedge_error_limit"]
    active, positions, reports = {}, {}, []
    previous = ledger[0]["ts"]

    def close(book, timestamp, unrealized=ZERO):
        episode = active.pop(book)
        reports.append(_record(episode, timestamp, unrealized, start, stop))

    for seq, row in enumerate(ledger):
        timestamp, event = row["ts"], row["event"]
        if row["seq"] != seq or type(timestamp) is not int or timestamp < previous:
            raise Rejected("invalid_episode_ledger_order", str(seq))
        previous = timestamp
        if event == "funding":
            book = row["book"]
            if book not in active or row["symbol"] not in positions[book]:
                raise Rejected("unattributed_funding", str(seq))
            active[book].funding += number(row["cash_delta"])
        if event in {"mark", "frame_end"}:
            for book, episode in active.items():
                _establish(episode, positions[book], timestamp, marks, tolerance)
        if event not in {"fill", "risk_fill"}:
            continue

        book, symbol = row["book"], row["symbol"]
        held = positions.setdefault(book, {})
        original = held.get(symbol, Position()).quantity
        delta, fee, price = (number(row[k]) for k in ("executed_delta", "fee", "execution_price"))
        if not delta or fee < 0 or price <= 0:
            raise Rejected("invalid_episode_fill", str(seq))
        crosses = original * (original + delta) < 0
        # A single fill crossing through zero can close a book only when every other leg is flat.
        split = crosses and set(held) == {symbol}
        parts = (-original, original + delta) if split else (delta,)
        remaining_fee, realized = fee, ZERO
        for part_index, part in enumerate(parts):
            if book not in active:
                signal = row["signal"]
                intent = intents.get(signal)
                if (
                    intent is None
                    or intent.book != book
                    or intent.known_at != row["known_at"]
                    or intent.known_at >= timestamp
                    or symbol not in intent.notionals
                    or number(intent.notionals[symbol]) * part <= 0
                ):
                    raise Rejected("missing_or_inconsistent_entry_intent", str(seq))
                active[book] = Episode(book, timestamp, signal, intent)
            episode = active[book]
            charge = fee * abs(part / delta) if split and part_index == 0 else remaining_fee
            remaining_fee -= charge
            new, pnl = fill_position(held.get(symbol, Position()), part, price)
            realized += pnl
            if new.quantity:
                held[symbol] = new
            else:
                held.pop(symbol)
            episode.realized += pnl
            episode.fees += charge
            episode.fill_parts.append(
                {
                    "seq": seq,
                    "symbol": symbol,
                    "quantity": str(part),
                    "fee": str(charge),
                    "realized_pnl": str(pnl),
                }
            )
            if held:
                _establish(episode, held, timestamp, marks, tolerance)
            else:
                close(book, timestamp)
        if realized != number(row["realized_delta"]) or sum(
            (p.quantity for s, p in held.items() if s == symbol), ZERO
        ) != number(row["target_quantity"]) - number(row["remaining_target_quantity"]):
            raise Rejected("episode_fill_reconciliation_failed", str(seq))

    terminal = {
        (p["book"], p["symbol"]): number(p["quantity"])
        for p in ledger[-1]["positions"]
        if number(p["quantity"])
    }
    if terminal != {
        (book, symbol): p.quantity for book, held in positions.items() for symbol, p in held.items()
    }:
        raise Rejected(
            "episode_terminal_reconciliation_failed", "Position snapshots disagree with actual fills"
        )
    for book in sorted(active):
        unrealized = sum(
            (
                p.quantity * (_mark(marks, stop - 1, symbol) - p.average_entry)
                for symbol, p in positions[book].items()
            ),
            ZERO,
        )
        close(book, None, unrealized)
    return {
        "status": "synthetic_diagnostic",
        "origin": ORIGIN,
        "performance_evidence": False,
        "eligible_days": days,
        "scenario": scenario,
        "episodes": reports,
        "qualifying_count": sum(r["status"] == "qualifying_round_trip" for r in reports),
    }


def qualifying_round_trips(*, origin, ledger, intents, marks, days, scenario):
    if origin != ORIGIN:
        return unavailable("real_episode_count_disabled")
    try:
        with localcontext() as context:
            context.prec = 34
            return _tally(ledger, intents, marks, daily_grid(days), scenario)
    except (KeyError, TypeError):
        return unavailable("missing_episode_input")
    except Rejected as exc:
        return unavailable(exc.code)

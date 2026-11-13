"""Independent USDT linear-perpetual accounting engine. No network or application imports."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, localcontext

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)


class Rejected(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def number(value):
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise Rejected("invalid_number", "Expected finite decimal data") from exc
    if not result.is_finite():
        raise Rejected("invalid_number", "Expected finite decimal data")
    return result


def sign(value):
    return ONE if value > 0 else -ONE if value < 0 else ZERO


@dataclass(frozen=True, order=True)
class Leg:
    book: str
    symbol: str


@dataclass(frozen=True)
class Frame:
    ts: int
    marks: dict[str, Decimal]
    execution: dict[str, Decimal]


@dataclass(frozen=True)
class Market:
    frames: tuple[Frame, ...]
    # Calendars are explicit inputs. Missing observations never redefine a calendar.
    frame_calendar: tuple[int, ...]
    execution_calendar: dict[str, tuple[int, ...]]
    funding_calendar: dict[str, tuple[int, ...]]
    realized_rates: dict[tuple[int, str], Decimal]
    provenance: str
    calendar_source: str

    def normalized(self):
        return replace(
            self,
            frames=tuple(
                Frame(
                    f.ts,
                    {s: number(p) for s, p in f.marks.items()},
                    {s: number(p) for s, p in f.execution.items()},
                )
                for f in self.frames
            ),
            realized_rates={key: number(rate) for key, rate in self.realized_rates.items()},
        )

    def validate(self):
        times = tuple(frame.ts for frame in self.frames)
        if not times or times != tuple(sorted(set(times))) or times != self.frame_calendar:
            raise Rejected("missing_frame", "Frames must exactly match the declared ordered calendar")
        if any(type(t) is not int for t in times):
            raise Rejected("invalid_time", "Timestamps must be integer milliseconds")
        universe = set(self.execution_calendar)
        if not universe or universe != set(self.funding_calendar):
            raise Rejected(
                "missing_calendar", "Every execution instrument requires an explicit funding calendar"
            )
        if not self.calendar_source or self.provenance not in {"synthetic_test", "okx_m1_snapshot"}:
            raise Rejected("missing_provenance", "Declare source and settlement schedule provenance")
        expected_rates = set()
        by_time = {frame.ts: frame for frame in self.frames}
        for symbol in sorted(universe):
            for calendar in (self.execution_calendar[symbol], self.funding_calendar[symbol]):
                if tuple(calendar) != tuple(sorted(set(calendar))) or any(
                    type(t) is not int or t not in by_time for t in calendar
                ):
                    raise Rejected("missing_frame", f"Invalid or unobserved calendar timestamp for {symbol}")
            for ts in self.execution_calendar[symbol]:
                if symbol not in by_time[ts].execution:
                    raise Rejected("missing_execution", f"Missing executable price: {symbol} at {ts}")
            expected_rates.update((ts, symbol) for ts in self.funding_calendar[symbol])
        if expected_rates != set(self.realized_rates):
            raise Rejected(
                "missing_funding", "Realized funding must exactly match the declared settlement calendar"
            )
        for value in self.realized_rates.values():
            number(value)  # Zero is valid; absent/NaN is not.
        for frame in self.frames:
            if set(frame.marks) != universe:
                raise Rejected(
                    "missing_mark", f"Missing contemporaneous settlement/valuation mark at {frame.ts}"
                )
            if set(frame.execution) != {s for s in universe if frame.ts in self.execution_calendar[s]}:
                raise Rejected(
                    "unexpected_execution", "Execution observations must exactly match the calendar"
                )
            if any(number(p) <= 0 for p in (*frame.marks.values(), *frame.execution.values())):
                raise Rejected("invalid_price", "Prices must be positive and finite")


@dataclass(frozen=True)
class Costs:
    fee_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("5")

    def validate(self):
        if not (0 <= number(self.fee_bps) < BPS and 0 <= number(self.slippage_bps) < BPS):
            raise Rejected("invalid_cost", "Fee and adverse slippage must be in [0,10000) bps")


@dataclass(frozen=True)
class Signal:
    id: str
    known_at: int
    targets: dict[Leg, Decimal]
    # Deterministic stress inputs, not an assertion of executable market liquidity.
    delay_steps: dict[Leg, int] = field(default_factory=dict)
    fill_fraction: dict[Leg, Decimal] = field(default_factory=dict)


@dataclass
class Position:
    quantity: Decimal = ZERO  # Signed base-asset units, NOT exchange contract counts.
    average_entry: Decimal = ZERO


@dataclass(frozen=True)
class Pending:
    ts: int
    signal: str
    known_at: int
    leg: Leg
    target: Decimal
    fraction: Decimal = ONE
    risk_exit: bool = False


def fill_position(position, delta, price):
    old = position.quantity
    new = old + delta
    realized = ZERO
    if old == 0 or sign(old) == sign(delta):
        average = (abs(old) * position.average_entry + abs(delta) * price) / abs(new) if new else ZERO
    else:
        realized = min(abs(old), abs(delta)) * (price - position.average_entry) * sign(old)
        average = ZERO if new == 0 else price if sign(new) != sign(old) else position.average_entry
    return Position(new, average), realized


class Engine:
    def __init__(self, market, initial="10000", costs=None):
        self.market = market
        self.initial = number(initial)
        self.costs = costs or Costs()
        self.positions = {}
        self.cash = self.initial
        self.realized = ZERO
        self.fees = ZERO
        self.slippage = ZERO
        self.funding = ZERO
        self.ledger = []
        self.halted = False
        self.risk_exit_unresolved = False
        self.used = False

    def valuation(self, marks, positions=None, cash=None):
        positions = self.positions if positions is None else positions
        cash = self.cash if cash is None else cash
        unrealized = sum(
            (p.quantity * (marks[leg.symbol] - p.average_entry) for leg, p in positions.items()), ZERO
        )
        asset_gross = defaultdict(lambda: ZERO)
        book_gross = defaultdict(lambda: ZERO)
        book_net = defaultdict(lambda: ZERO)
        book_legs = defaultdict(int)
        net = ZERO
        for leg, position in positions.items():
            notional = position.quantity * marks[leg.symbol]
            asset_gross[leg.symbol] += abs(notional)  # No netting between books sharing an asset.
            book_gross[leg.book] += abs(notional)
            book_net[leg.book] += notional
            book_legs[leg.book] += position.quantity != 0
            net += notional
        return {
            "equity": cash + unrealized,
            "unrealized_pnl": unrealized,
            "gross_notional": sum(asset_gross.values(), ZERO),
            "net_notional": net,
            "asset_absolute_notional": dict(sorted(asset_gross.items())),
            "book_gross_notional": dict(sorted(book_gross.items())),
            "book_net_notional": dict(sorted(book_net.items())),
            "single_leg_books": sorted(book for book, count in book_legs.items() if count == 1),
        }

    @staticmethod
    def breached(value):
        equity = value["equity"]
        return value["gross_notional"] > equity or any(
            gross > equity * Decimal("0.25") for gross in value["asset_absolute_notional"].values()
        )

    def record(self, frame, event, **detail):
        value = self.valuation(frame.marks)
        self.ledger.append(
            {
                "seq": len(self.ledger),
                "ts": frame.ts,
                "event": event,
                "cash": self.cash,
                "realized_pnl": self.realized,
                "cumulative_fees": self.fees,
                "cumulative_slippage_diagnostic": self.slippage,
                "cumulative_funding_cashflow": self.funding,
                **value,
                "positions": [
                    {
                        "book": leg.book,
                        "symbol": leg.symbol,
                        "quantity": p.quantity,
                        "average_entry": p.average_entry,
                    }
                    for leg, p in sorted(self.positions.items())
                ],
                **detail,
            }
        )

    def compile(self, signals):
        pending = []
        seen = set()
        previous = {}
        for signal in sorted(signals, key=lambda s: (s.known_at, s.id)):
            if (
                not signal.id
                or signal.id in seen
                or type(signal.known_at) is not int
                or signal.known_at < self.market.frames[0].ts
                or not signal.targets
            ):
                raise Rejected(
                    "invalid_signal", "Signals require unique IDs, known timestamps and explicit targets"
                )
            if set(signal.delay_steps) - set(signal.targets) or set(signal.fill_fraction) - set(
                signal.targets
            ):
                raise Rejected("invalid_signal", "Stress inputs must refer to an explicit target")
            seen.add(signal.id)
            for leg, target in sorted(signal.targets.items()):
                if not leg.book or leg.symbol not in self.market.execution_calendar:
                    raise Rejected("invalid_leg", "Unknown instrument or missing book identity")
                delay = signal.delay_steps.get(leg, 0)
                fraction = number(signal.fill_fraction.get(leg, ONE))
                if type(delay) is not int or delay < 0 or not ZERO <= fraction <= ONE:
                    raise Rejected(
                        "invalid_stress", "Delay must be nonnegative ticks and fill fraction in [0,1]"
                    )
                times = self.market.execution_calendar[leg.symbol]
                index = bisect_right(times, signal.known_at) + delay
                if index >= len(times):
                    raise Rejected("missing_execution", "No next execution tick within the supplied window")
                ts = times[index]
                if signal.known_at <= previous.get(leg, -1):
                    raise Rejected(
                        "overlapping_signal", "A later decision must follow the previous leg's fill tick"
                    )
                previous[leg] = ts
                pending.append(Pending(ts, signal.id, signal.known_at, leg, number(target), fraction))
        return pending

    def execute(self, frame, order):
        old = self.positions.get(order.leg, Position())
        requested = order.target - old.quantity
        delta = requested * order.fraction
        detail = {
            "signal": order.signal,
            "book": order.leg.book,
            "symbol": order.leg.symbol,
            "known_at": order.known_at,
            "target_quantity": order.target,
            "requested_delta": requested,
        }
        if delta == 0:
            self.record(frame, "unfilled" if requested else "no_change", **detail)
            return
        reference = frame.execution[order.leg.symbol]
        price = reference * (ONE + sign(delta) * number(self.costs.slippage_bps) / BPS)
        fee = abs(delta) * price * number(self.costs.fee_bps) / BPS
        position, pnl = fill_position(old, delta, price)
        projected = {**self.positions, order.leg: position}
        value = self.valuation(frame.marks, projected, self.cash + pnl - fee)
        reducing = abs(position.quantity) < abs(old.quantity) and (
            position.quantity == 0 or sign(position.quantity) == sign(old.quantity)
        )
        if value["equity"] <= 0 or (self.breached(value) and not reducing):
            self.record(frame, "rejected_risk", **detail, projected_equity=value["equity"])
            return
        self.positions = projected
        self.cash += pnl - fee
        self.realized += pnl
        self.fees += fee
        slip = abs(delta) * abs(price - reference)
        self.slippage += slip  # Embedded in execution price: never subtract a second time.
        self.record(
            frame,
            "risk_fill" if order.risk_exit else "fill",
            **detail,
            executed_delta=delta,
            execution_price=price,
            reference_price=reference,
            fee=fee,
            realized_delta=pnl,
            slippage_diagnostic=slip,
            remaining_target_quantity=order.target - position.quantity,
        )

    def run(self, signals=()):
        # The precision belongs to this run, not the process or application.
        with localcontext() as context:
            context.prec = 34
            return self._run(signals)

    def _run(self, signals):
        if self.used:
            raise Rejected("engine_reuse", "Each engine instance represents exactly one ledger")
        self.used = True
        self.market = self.market.normalized()
        self.market.validate()  # Validate ALL observations before any accounting mutation.
        self.costs.validate()
        if self.initial <= 0:
            raise Rejected("invalid_capital", "Initial equity must be positive")
        pending = self.compile(signals)
        risk_pending = []
        for frame in self.market.frames:
            self.record(frame, "mark")
            # Positions held immediately BEFORE this timestamp pay; opening fills occur afterwards.
            for leg, position in sorted(self.positions.items()):
                if position.quantity and frame.ts in self.market.funding_calendar[leg.symbol]:
                    rate = number(self.market.realized_rates[frame.ts, leg.symbol])
                    flow = -position.quantity * frame.marks[leg.symbol] * rate
                    self.cash += flow
                    self.funding += flow
                    self.record(
                        frame,
                        "funding",
                        book=leg.book,
                        symbol=leg.symbol,
                        rate=rate,
                        settlement_mark=frame.marks[leg.symbol],
                        cash_delta=flow,
                    )
            value = self.valuation(frame.marks)
            if value["equity"] <= 0:
                raise Rejected(
                    "insolvent", "Equity exhausted; no liquidation price or recovery is fabricated"
                )
            if self.breached(value) and not self.halted:
                self.halted = True
                self.record(
                    frame,
                    "risk_halt",
                    cancelled_pending=len(pending),
                    cancelled_known_pending=len([p for p in pending if p.known_at <= frame.ts]),
                    cancelled_preloaded_future_signals=len([p for p in pending if p.known_at > frame.ts]),
                )
                pending = []  # No stale hedge leg may open after an emergency flatten.
                for leg, position in sorted(self.positions.items()):
                    if not position.quantity:
                        continue
                    times = self.market.execution_calendar[leg.symbol]
                    index = bisect_right(times, frame.ts)
                    if index == len(times):
                        self.risk_exit_unresolved = True
                    else:
                        risk_pending.append(
                            Pending(times[index], "risk_exit", frame.ts, leg, ZERO, risk_exit=True)
                        )
            due = [p for p in risk_pending + pending if p.ts == frame.ts]
            for order in sorted(due, key=lambda p: (not p.risk_exit, p.known_at, p.signal, p.leg)):
                self.execute(frame, order)
            pending = [p for p in pending if p.ts > frame.ts]
            risk_pending = [p for p in risk_pending if p.ts > frame.ts]
            self.record(frame, "frame_end", risk_halted=self.halted)
        final = self.valuation(self.market.frames[-1].marks)
        self.risk_exit_unresolved |= self.halted and any(p.quantity for p in self.positions.values())
        return {
            "scope": "accounting_diagnostic_only",
            "provenance": self.market.provenance,
            "initial_equity": self.initial,
            "cash": self.cash,
            **final,
            "realized_pnl": self.realized,
            "funding_cashflow": self.funding,
            "fees": self.fees,
            "slippage_diagnostic": self.slippage,
            "reconciliation_error": final["equity"]
            - (self.initial + self.realized + final["unrealized_pnl"] + self.funding - self.fees),
            "risk_halted": self.halted,
            "risk_exit_unresolved": self.risk_exit_unresolved,
            "full_research_conclusion_permitted": False,
            "ledger": self.ledger,
        }


def require_full_research(market):
    raise Rejected(
        "full_research_unavailable",
        f"M2 is an accounting engine only ({market.provenance}); long-history funding, marks, "
        "point-in-time universe and locked out-of-sample evidence remain required",
    )


def serializable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value

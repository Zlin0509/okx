"""Causal weekly entries and daily exits for explicit synthetic-qualified relations."""

import math
from collections import defaultdict
from dataclasses import asdict, dataclass

from .decision_view import Decision
from .engine import ZERO, Leg, Rejected, Signal, number
from .spreads import fit_model

DAY = 86400000


@dataclass(frozen=True)
class Relation:
    book: str
    symbols: tuple[str, ...]
    qualification: object


def hedge_error(model, notionals):
    """L1 distance from best frozen hedge-vector shape, divided by actual gross."""
    values = [float(notionals.get(s, 0)) for s in model.symbols]
    vector = model.vector
    gross = math.fsum(abs(x) for x in values)
    if not gross:
        return 1.0
    k = math.fsum(a * b for a, b in zip(values, vector, strict=True)) / math.fsum(v * v for v in vector)
    return math.fsum(abs(a - k * b) for a, b in zip(values, vector, strict=True)) / gross


def allocate(proposals, reserved, equity, params):
    """Uniformly scale new proposals; never net absolute shared-symbol exposure."""
    asset = defaultdict(lambda: ZERO)
    for leg, notional in reserved.items():
        asset[leg.symbol] += abs(notional)
    gross = sum(asset.values(), ZERO)
    requested = defaultdict(lambda: ZERO)
    for targets in proposals.values():
        for symbol, notional in targets.items():
            requested[symbol] += abs(notional)
    total = sum(requested.values(), ZERO)
    if not total:
        return {}, ZERO
    scale = min(number(1), max(ZERO, (equity * number(params["allocation_gross_limit"]) - gross) / total))
    for symbol, amount in requested.items():
        if not amount:
            continue
        scale = min(
            scale, max(ZERO, (equity * number(params["allocation_asset_limit"]) - asset[symbol]) / amount)
        )
    return {
        book: {s: n * scale for s, n in targets.items()}
        for book, targets in proposals.items()
        if any(targets.values())
    }, scale


class Portfolio:
    def __init__(self, relations, params, stress=None):
        if len({r.book for r in relations}) != len(relations):
            raise Rejected("invalid_relation", "Book names must be unique")
        self.relations = tuple(sorted(relations, key=lambda r: r.book))
        self.params = params
        self.stress = stress or {}
        self.active = {}
        self.events = []
        self.counter = 0
        self.invalidated = set()

    def event(self, ts, book, action, reason, **detail):
        self.events.append({"ts": ts, "book": book, "action": action, "reason": reason, **detail})

    def signal(self, ts, book, targets, entry=False):
        self.counter += 1
        stress = self.stress.get(book, {}) if entry else {}
        return Signal(
            f"m3-{self.counter}",
            ts,
            targets,
            {Leg(book, s): value for s, value in stress.get("delay_steps", {}).items()},
            {Leg(book, s): number(value) for s, value in stress.get("fill_fraction", {}).items()},
        )

    def __call__(self, frame):
        if frame.ts % DAY != DAY - 1:
            return Decision()
        ts = frame.ts
        day = ts // DAY
        current = frame.observations.get(ts)
        available = (
            current is not None
            and current.ts == ts
            and current.available_at <= ts
            and current.available_at >= current.ts
        )
        orders = []
        cancel_books = set()
        exited = set()
        for book, state in list(self.active.items()):
            held = {
                leg.symbol: p.quantity * frame.marks[leg.symbol]
                for leg, p in frame.positions.items()
                if leg.book == book and p.quantity
            }
            busy = any(p.leg.book == book for p in frame.pending)
            if state["exiting"]:
                if not held and not busy:
                    del self.active[book]
                continue
            fills = [
                row
                for row in frame.fills
                if row.leg.book == book and row.ts > state["known_at"] and row.executed_delta
            ]
            first_fill = fills[0].ts if fills else None
            error = hedge_error(state["model"], held)
            reason, z = None, None
            try:
                if not available:
                    raise Rejected("missing_observation", "Current close is missing or unavailable")
                z = state["model"].zscore(current.prices)
            except Rejected:
                reason = "missing_observation"
            if reason is None:
                if abs(z) >= self.params["break_z"]:
                    reason = "relationship_failure"
                elif error > self.params["hedge_error_limit"]:
                    reason = "hedge_failure"
                elif abs(z) <= self.params["exit_z"] or z * state["entry_z"] <= 0:
                    reason = "mean_reversion"
                elif first_fill is not None and ts - first_fill >= self.params["timeout_days"] * DAY:
                    reason = "timeout"
            self.event(
                ts,
                book,
                "daily_check",
                reason or "hold",
                z=z,
                hedge_error=error,
                first_fill_ts=first_fill,
                net_notional=sum(held.values(), ZERO),
            )
            if reason:
                if reason == "relationship_failure":
                    self.invalidated.add(book)
                cancel_books.add(book)
                orders.append(self.signal(ts, book, {Leg(book, s): ZERO for s in state["model"].symbols}))
                self.event(ts, book, "exit", reason, z=z, hedge_error=error)
                state["exiting"] = True
                exited.add(book)
        # New entries only weekly. Daily exits above do not wait for that schedule.
        if day % self.params["decision_every_days"] == 0 and available:
            proposals, models = {}, {}
            equity = frame.equity
            reserved = {
                Leg(leg.book, leg.symbol): abs(position.quantity * frame.marks[leg.symbol])
                for leg, position in frame.positions.items()
                if position.quantity
            }
            for state in self.active.values():
                if not state["exiting"]:
                    for leg, quantity in state["targets"].items():
                        reserved[leg] = max(reserved.get(leg, ZERO), abs(quantity * frame.marks[leg.symbol]))
            for relation in self.relations:
                if relation.book in self.invalidated:
                    self.event(ts, relation.book, "rejected", "relationship_invalidated")
                    continue
                if relation.book in self.active or relation.book in exited:
                    continue
                try:
                    model = fit_model(
                        tuple(frame.observations[t] for t in sorted(frame.observations)),
                        relation.symbols,
                        ts,
                        relation.qualification,
                        self.params["window"],
                    )
                    z = model.zscore(current.prices)
                    if not self.params["entry_z"] <= abs(z) < self.params["break_z"]:
                        self.event(ts, relation.book, "no_entry", "deviation_outside_entry", z=z)
                        continue
                    direction = -1 if z > 0 else 1
                    vector = model.vector
                    norm = number(math.fsum(abs(v) for v in vector))
                    size = equity * number(self.params["book_gross_fraction"])
                    proposals[relation.book] = {
                        s: number(v) * direction * size / norm
                        for s, v in zip(model.symbols, vector, strict=True)
                    }
                    models[relation.book] = (model, z)
                except Rejected as exc:
                    self.event(ts, relation.book, "rejected", exc.code)
            allocations, scale = allocate(proposals, reserved, equity, self.params)
            for book, notionals in allocations.items():
                if not scale:
                    self.event(ts, book, "rejected", "allocation_capacity")
                    continue
                model, z = models[book]
                targets = {
                    Leg(book, s): amount / number(current.prices[s]) for s, amount in notionals.items()
                }
                orders.append(self.signal(ts, book, targets, entry=True))
                self.active[book] = {
                    "model": model,
                    "entry_z": z,
                    "known_at": ts,
                    "targets": targets,
                    "exiting": False,
                }
                self.event(
                    ts,
                    book,
                    "entry",
                    "synthetic_truth_only",
                    z=z,
                    model=asdict(model),
                    allocation_scale=scale,
                    targets=[{"symbol": leg.symbol, "quantity": q} for leg, q in targets.items()],
                )
        return Decision(tuple(orders), frozenset(cancel_books))


def holding_periods(ledger):
    """Actual first nonzero fill to complete flatten; open periods are censored."""
    periods, active = [], {}
    for row in ledger:
        if row["event"] not in {"fill", "risk_fill"}:
            continue
        book = row["book"]
        held = any(p["quantity"] for p in row["positions"] if p["book"] == book)
        if held and book not in active:
            active[book] = row["ts"]
        if not held and book in active:
            start = active.pop(book)
            periods.append(
                {
                    "book": book,
                    "first_fill": start,
                    "flat_at": row["ts"],
                    "held_days": (row["ts"] - start) / DAY,
                    "right_censored": False,
                }
            )
    for book, start in active.items():
        periods.append(
            {
                "book": book,
                "first_fill": start,
                "flat_at": None,
                "held_days": (ledger[-1]["ts"] - start) / DAY,
                "right_censored": True,
            }
        )
    return periods

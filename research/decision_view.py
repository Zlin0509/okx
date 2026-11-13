"""Detached, deeply read-only public decision data. Not a Python security sandbox."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from .engine import Signal, number


@dataclass(frozen=True, slots=True, order=True)
class VisibleLeg:
    book: str
    symbol: str


@dataclass(frozen=True, slots=True)
class Observed:
    ts: int
    available_at: int
    prices: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class Held:
    quantity: Decimal
    average_entry: Decimal


@dataclass(frozen=True, slots=True)
class Filled:
    ts: int
    known_at: int
    leg: VisibleLeg
    executed_delta: Decimal


@dataclass(frozen=True, slots=True)
class KnownIntent:
    # Future fill timestamps/prices/calendars are deliberately not decision data.
    leg: VisibleLeg
    known_at: int
    target: Decimal


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    ts: int
    marks: Mapping[str, Decimal]
    execution: Mapping[str, Decimal]
    observations: Mapping[int, Observed]
    positions: Mapping[VisibleLeg, Held]
    fills: tuple[Filled, ...]
    pending: tuple[KnownIntent, ...]
    equity: Decimal


@dataclass(frozen=True, slots=True)
class Decision:
    signals: tuple[Signal, ...] = ()
    cancel_books: frozenset[str] = frozenset()


def observed_copy(row):
    return Observed(row.ts, row.available_at, MappingProxyType({s: number(p) for s, p in row.prices.items()}))


def snapshot(frame, engine, pending, observed):
    """Driver-only constructor: no source, engine or source-linked objects in the result."""
    return DecisionSnapshot(
        ts=frame.ts,
        marks=MappingProxyType(dict(frame.marks)),
        execution=MappingProxyType(dict(frame.execution)),
        observations=MappingProxyType(dict(observed)),
        positions=MappingProxyType(
            {
                VisibleLeg(leg.book, leg.symbol): Held(p.quantity, p.average_entry)
                for leg, p in engine.positions.items()
            }
        ),
        fills=tuple(
            Filled(row["ts"], row["known_at"], VisibleLeg(row["book"], row["symbol"]), row["executed_delta"])
            for row in engine.ledger
            if row["event"] == "fill"
        ),
        pending=tuple(
            KnownIntent(VisibleLeg(p.leg.book, p.leg.symbol), p.known_at, p.target) for p in pending
        ),
        equity=engine.valuation(frame.marks)["equity"],
    )

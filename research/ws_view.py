"""Detached read-only market view, with no fabricated account or execution fields."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .decision_view import Observed
from .engine import Rejected, number


def readonly(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: readonly(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(readonly(v) for v in value)
    return value


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class MarketView:
    available_ns: int
    origin: str
    connected: bool
    components: Mapping
    fresh_marks: Mapping
    missing_or_stale_marks: tuple[str, ...]
    exchange_mark_skew_ms: int | None

    @property
    def daily_observations(self):
        # Receive-time aligned quotes are not confirmed daily candles.
        return ()

    def intraday_observation(self):
        if not self.connected or self.missing_or_stale_marks:
            raise Rejected("incomplete_intraday_snapshot", "Every required mark must be fresh and connected")
        ms = (self.available_ns + 999_999) // 1_000_000
        return Observed(ms, ms, MappingProxyType({s: number(p) for s, p in self.fresh_marks.items()}))

    def require_model(self):
        raise Rejected(
            "historical_qualification_unknown",
            "No complete daily history or point-in-time listing/universe qualification; real statistical permission remains disabled",
        )

    def serialize(self):
        return {
            "available_ns": self.available_ns,
            "origin": self.origin,
            "connected": self.connected,
            "components": plain(self.components),
            "fresh_marks": plain(self.fresh_marks),
            "missing_or_stale_marks": list(self.missing_or_stale_marks),
            "exchange_mark_skew_ms": self.exchange_mark_skew_ms,
            "research_timeframe": "receive-asof grid; not daily bars or a common exchange timestamp",
            "complete_daily_observations": 0,
            "model_permission": "refused",
            "account_equity": None,
            "execution_prices": None,
        }

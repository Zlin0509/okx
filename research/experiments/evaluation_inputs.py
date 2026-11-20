"""Shared daily calendar and result types for the synthetic acceptance checks."""

from dataclasses import dataclass

from ..engine import Rejected
from ..portfolio import DAY

ORIGIN = "synthetic_test"


def unavailable(reason):
    return {
        "status": "unavailable",
        "reason": reason,
        "real_returns": None,
        "performance_evidence": False,
        "acceptance_passed": False,
    }


def daily_grid(days):
    days = tuple(days)
    if not days or any(type(day) is not int or day % DAY for day in days):
        raise Rejected("missing_eligible_dates", "Explicit UTC date grid required")
    if any(b - a != DAY for a, b in zip(days, days[1:], strict=False)):
        raise Rejected("noncontiguous_eligible_dates", "Daily paths cannot compress missing dates")
    return days


@dataclass(frozen=True)
class DailyReturns:
    """Each value is the return through that date's UTC close, including cash days."""

    origin: str
    scenario: str
    capital_usdt: int
    days: tuple[int, ...]
    values: tuple[float, ...]

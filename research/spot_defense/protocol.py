"""Frozen ZLIN-21 v1 constants and UTC boundaries."""

from datetime import UTC, date, datetime, time, timedelta

SYMBOLS = ("BTC-USDT", "ETH-USDT")
WARMUP_START = date(2023, 7, 1)
WITNESS_DAY = WARMUP_START - timedelta(days=1)
TRAIN_START = date(2023, 10, 1)
VALIDATION_START = date(2025, 1, 1)
LOCK_START = date(2025, 7, 1)
LOCK_END = date(2026, 10, 31)
WINDOWS = {
    "train": (TRAIN_START, VALIDATION_START),
    "validation": (VALIDATION_START, LOCK_START),
}
CHECK_HOUR, EXEC_HOUR = 1, 2
DAY = timedelta(days=1)
INITIAL = 10_000.0
TREND_DAYS = 60
LIQUIDITY_DAYS = 30
MIN_TURNOVER = 10_000_000
TARGET = 0.125
ASSET_CAP, TOTAL_CAP = 0.15, 0.30
COST_BPS = (20, 40, 60)
MAX_DRAWDOWN, MIN_ROLLING_30, MAX_VOLATILITY, MAX_TURNOVER = 0.12, -0.07, 0.12, 6.0


def at(day: date, hour: int = 0) -> datetime:
    return datetime.combine(day, time(hour), UTC)


def days(start: date, end: date):
    for offset in range((end - start).days):
        yield start + offset * DAY

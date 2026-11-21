"""Verify the pre-lock snapshot and expose point-in-time market observations."""

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .collect import COINBASE, NOTICE_SOURCES, OKX, OKX_INTERVALS, coinbase_rows, okx_rows, sha256
from .protocol import DAY, EXEC_HOUR, LOCK_START, SYMBOLS, WITNESS_DAY, at, days

FX = "USDT-USD"


@dataclass(frozen=True)
class Bar:
    close: float
    turnover: float
    available: datetime


@dataclass(frozen=True)
class Interruption:
    start: datetime
    end: datetime
    known: datetime
    symbols: tuple[str, ...] = SYMBOLS
    stops_trading: bool = True

    def applies(self, symbol, start, end, *, as_of=None):
        return (
            self.stops_trading
            and symbol in self.symbols
            and self.start < end
            and start < self.end
            and (as_of is None or self.known <= as_of)
        )


@dataclass
class Market:
    daily: dict[str, dict[date, Bar]] = field(
        default_factory=lambda: {symbol: {} for symbol in (*SYMBOLS, FX)}
    )
    opens: dict[str, dict[date, float]] = field(default_factory=lambda: {symbol: {} for symbol in SYMBOLS})
    listed: frozenset[str] = frozenset()
    interruptions: tuple[Interruption, ...] = ()

    def bar(self, symbol, day, as_of):
        bar = self.daily[symbol].get(day)
        if bar and at(day + DAY) <= bar.available <= as_of:
            return bar
        return None

    def recent(self, symbol, day, count, as_of):
        return [self.bar(symbol, past, as_of) for past in days(day - count * DAY, day)]

    def execution_price(self, symbol, day):
        start = at(day, EXEC_HOUR)
        end = start + timedelta(hours=1)
        if any(item.applies(symbol, start, end) for item in self.interruptions):
            return None
        return self.opens[symbol].get(day)

    def known_interruption(self, symbol, day, as_of):
        start = at(day, EXEC_HOUR)
        return any(
            item.applies(symbol, as_of, start + timedelta(hours=1), as_of=as_of)
            for item in self.interruptions
        )


def _num(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid exchange numeric field")
    return result


def _record(series, timestamp, bar, target):
    if timestamp in target and bar != target[timestamp]:
        raise ValueError(f"conflicting candles in {series}: {timestamp}")
    target[timestamp] = bar


def _venue_events(notices):
    # Report dates are conservative upper bounds for publication. Scope and hours come from the reports.
    lifetime = [
        ("incident_july", "2023-07-05T08:20:00", "2023-07-05T10:00:00", "2023-07-08", True),
        ("incident_august", "2023-08-14T06:14:09", "2023-08-14T06:36:39", "2023-08-17", True),
        ("incident_september", "2023-09-13T07:38:00", "2023-09-13T07:43:00", "2023-09-21", True),
        ("incident_rest", "2024-03-27T11:39:00", "2024-03-27T14:09:00", "2024-03-29", False),
    ]
    return tuple(
        Interruption(
            *(datetime.fromisoformat(value).replace(tzinfo=UTC) for value in (start, end, known)),
            stops_trading=stops,
        )
        for notice, start, end, known, stops in lifetime
        if notice in notices
    )


def load(root: Path, expected_sha256: str):
    body = (root / "manifest.json").read_bytes()
    if sha256(body) != expected_sha256:
        raise ValueError("manifest SHA-256 mismatch")
    manifest = json.loads(body)
    if (manifest["schema"], manifest["timezone"], manifest["end_exclusive"]) != (
        1,
        "UTC",
        LOCK_START.isoformat(),
    ) or manifest["first_requested_date"] != WITNESS_DAY.isoformat():
        raise ValueError("invalid snapshot boundaries")

    market = Market()
    files = set()
    notices = set()
    all_hours = {symbol: {} for symbol in SYMBOLS}
    for item in manifest["entries"]:
        path = Path(item["file"])
        if (
            path.parent != Path("raw")
            or path in files
            or not (root / path).resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("invalid or repeated raw file path")
        files.add(path)
        body = (root / path).read_bytes()
        if len(body) != item["bytes"] or sha256(body) != item["sha256"]:
            raise ValueError(f"raw response mismatch: {path}")
        series, url = item["series"], item["url"]
        endpoint, _, query = url.partition("?")
        params = parse_qs(urlsplit(url).query)
        if series in NOTICE_SOURCES:
            if url != NOTICE_SOURCES[series] or not body:
                raise ValueError(f"wrong notice source: {series}")
            notices.add(series)
        elif series == FX:
            lower, upper = item["lower"], item["upper"]
            if (
                endpoint != COINBASE
                or params["granularity"] != ["86400"]
                or int(datetime.fromisoformat(params["start"][0]).timestamp()) != lower
                or int(datetime.fromisoformat(params["end"][0]).timestamp()) != upper
                or upper >= at(LOCK_START).timestamp()
                or not query
            ):
                raise ValueError("wrong Coinbase source or range")
            for ts, low, high, open_, close, volume in coinbase_rows(body, upper):
                instant = datetime.fromtimestamp(int(ts), UTC)
                if instant != at(instant.date()):
                    raise ValueError("non-UTC-daily Coinbase candle")
                if int(ts) < lower:
                    # Coinbase documents possible rows from before the requested interval.
                    continue
                low, high, open_, close, volume = map(_num, (low, high, open_, close, volume))
                if 0 < low <= min(open_, close) <= max(open_, close) <= high and volume > 0:
                    _record(series, instant.date(), Bar(close, volume, instant + DAY), market.daily[FX])
        else:
            symbol, bar = series.split(":")
            cursor = item["cursor"]
            if (
                endpoint != OKX
                or symbol not in SYMBOLS
                or bar not in OKX_INTERVALS
                or params["instId"] != [symbol]
                or params["bar"] != [bar]
                or params["after"] != [str(cursor)]
                or cursor > at(LOCK_START).timestamp() * 1000
            ):
                raise ValueError("wrong OKX source or range")
            for ts, open_, high, low, close, volume, base, quote, confirm in okx_rows(body, cursor):
                ts = int(ts)
                if ts % OKX_INTERVALS[bar]:
                    raise ValueError(f"misaligned OKX candle: {series}")
                instant = datetime.fromtimestamp(ts / 1000, UTC)
                if instant.date() < WITNESS_DAY:
                    continue
                open_, high, low, close, volume, base, quote = map(
                    _num, (open_, high, low, close, volume, base, quote)
                )
                if not (
                    confirm == "1"
                    and 0 < low <= min(open_, close) <= max(open_, close) <= high
                    and min(volume, base, quote) > 0
                ):
                    continue
                if bar == "1Dutc":
                    _record(series, instant.date(), Bar(close, quote, instant + DAY), market.daily[symbol])
                else:
                    _record(series, instant, open_, all_hours[symbol])
                    if instant.hour == EXEC_HOUR:
                        _record(series, instant.date(), open_, market.opens[symbol])

    market.listed = frozenset(
        symbol
        for symbol, source in zip(SYMBOLS, ("listing_btc", "listing_eth"), strict=True)
        if source in notices and WITNESS_DAY in market.daily[symbol]
    )
    market.interruptions = _venue_events(notices)
    return market, {
        "manifest_sha256": expected_sha256,
        "raw_files": len(files),
        "qualified_listing_witnesses": sorted(market.listed),
        "notice_snapshots": sorted(notices),
        "hours_with_trades": {symbol: len(all_hours[symbol]) for symbol in SYMBOLS},
        "whole_status_history_verified": False,
        "historical_daily_publication_latency_verified": False,
        "prior_exposure_isolation_verified": False,
    }

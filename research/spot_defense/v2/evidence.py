"""Validate a custodian's offline, normalized point-in-time evidence."""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ..data import FX, Bar, Market
from ..protocol import CHECK_HOUR, DAY, EXEC_HOUR, SYMBOLS, at, days
from .calendar import END, EVAL_START, WARMUP_START, bounds, observation_day

AVAILABILITY = {**dict.fromkeys(SYMBOLS, "okx:1Dutc"), FX: "coinbase-exchange:86400"}
STATE_SOURCE = {
    **dict.fromkeys(SYMBOLS, "okx:instrument+system+announcements"),
    FX: "coinbase-exchange:product",
}
TRADE_SOURCE = "okx:trades-all"
FILES = {"availability.json", "provenance.json", "status.json", "trades.json"}
LIVE = {**dict.fromkeys(SYMBOLS, "live"), FX: "online"}
STATES = {"live", "online", "suspend", "offline", "delisted", "preopen", "maintenance", "unknown"}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def checksum(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("invalid SHA-256")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def unpack(body):
    return json.loads(body, object_pairs_hook=_object)


def fields(row, expected):
    if not isinstance(row, dict) or set(row) != set(expected.split()):
        raise ValueError(f"expected fields: {expected}")


def instant(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value) is None
    ):
        raise ValueError("expected UTC Z timestamp")
    return datetime.fromisoformat(value)


def _day(value, *, execution=False):
    day = observation_day(date.fromisoformat(value), execution=execution)
    if value != day.isoformat():
        raise ValueError("expected ISO UTC day")
    return day


def _epoch(value):
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[0-9]+", value)):
        return int(value)
    raise ValueError("invalid exchange timestamp")


def _number(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("nonpositive or nonfinite observation")
    return result


def _opaque_id(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing evidence identifier")
    return value


def _record(target, key, value):
    if key in target:
        raise ValueError("duplicate evidence key")
    target[key] = value


@dataclass(frozen=True)
class Trade:
    symbol: str
    trade_id: str
    timestamp: datetime
    received: datetime
    price: float
    stream: str


@dataclass(frozen=True)
class Span:
    start: datetime
    end: datetime
    value: str


def _covers(spans, start, end, *, value=None):
    """Cover the entire half-open interval using consecutive witnessed spans."""
    cursor = start
    for span in spans:
        if span.end <= cursor:
            continue
        if span.start > cursor or (value is not None and span.value != value):
            return False
        cursor = span.end
        if cursor >= end:
            return True
    return False


@dataclass
class Evidence(Market):
    checks: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)
    feeds: dict = field(default_factory=dict)
    trades: dict = field(default_factory=dict)

    def bar(self, symbol, day, as_of):
        bar = super().bar(symbol, day, as_of)
        return bar if bar and bar.available < min(as_of, at(day + DAY, CHECK_HOUR)) else None

    def live_at_check(self, symbol, day):
        return self.checks.get((day, symbol)) == LIVE[symbol]

    def known_interruption(self, symbol, day, as_of):
        return not self.live_at_check(symbol, day)

    def continuous(self, key, through, *, stream=None, require_live=True):
        day, symbol = key
        start = at(day, EXEC_HOUR)
        stop = through + timedelta(microseconds=1)
        if not _covers(self.states.get(key, ()), start, stop, value=LIVE[symbol] if require_live else None):
            return False
        return symbol == FX or _covers(self.feeds.get(key, ()), start, stop, value=stream)

    def first_trade(self, day, symbol):
        key = day, symbol
        observed = self.trades.get(key, ())
        if not observed:
            return None
        first = min(observed, key=lambda item: (item.timestamp, item.received))
        # A later exchange print received before the earliest print makes the first event ambiguous.
        if any(
            item.received < first.received
            or (item != first and (item.timestamp, item.received) == (first.timestamp, first.received))
            for item in observed
        ):
            return None
        if not self.continuous(key, first.received, stream=first.stream):
            return None
        return first

    def coverage(self):
        missing = {"daily": [], "check_state": [], "execution_state": [], "trade_feed": [], "first_trade": []}
        for day in days(WARMUP_START, END):
            check = at(day + DAY, CHECK_HOUR)
            for symbol in AVAILABILITY:
                if not self.bar(symbol, day, check):
                    missing["daily"].append([str(day), symbol])
        for day in days(EVAL_START, END):
            start, end = at(day, EXEC_HOUR), at(day, EXEC_HOUR + 1)
            for symbol in AVAILABILITY:
                key = day, symbol
                if self.checks.get(key, "unknown") == "unknown":
                    missing["check_state"].append([str(day), symbol])
                states = self.states.get(key, ())
                if not _covers(states, start, end) or any(span.value == "unknown" for span in states):
                    missing["execution_state"].append([str(day), symbol])
                if symbol in SYMBOLS:
                    # A documented halt or a continuously witnessed quiet market can have no trade.
                    feed = self.feeds.get(key, ())
                    if not feed or not _covers(feed, start, end, value=feed[0].value):
                        missing["trade_feed"].append([str(day), symbol])
                    observed = self.trades.get(key, ())
                    if observed and self.first_trade(day, symbol) is None:
                        first = min(observed, key=lambda item: (item.timestamp, item.received))
                        if _covers(
                            states, start, first.received + timedelta(microseconds=1), value=LIVE[symbol]
                        ):
                            missing["first_trade"].append([str(day), symbol])
        return {"bounds": bounds(), "missing": missing, "complete": not any(missing.values())}


def _clean_file(root, name):
    file = root / name
    if file.is_symlink() or not file.is_file():
        raise ValueError(f"expected plain evidence file: {name}")
    return file.read_bytes()


def inspect_manifest(body, *, expected_hash):
    """Read coverage metadata only. No path in the manifest is opened here."""
    if digest(body) != checksum(expected_hash):
        raise ValueError("manifest SHA-256 mismatch")
    obj = unpack(body)
    fields(obj, "schema bounds files missing_counts source_manifest_sha256")
    checksum(obj["source_manifest_sha256"])
    if obj["schema"] != "zlin21-v2-evidence-1" or obj["bounds"] != bounds() or set(obj["files"]) != FILES:
        raise ValueError("invalid v2 manifest or window")
    for item in obj["files"].values():
        fields(item, "bytes sha256 records")
        if any(type(item[key]) is not int or item[key] < 0 for key in ("bytes", "records")):
            raise ValueError("invalid file size or record count")
        checksum(item["sha256"])
    fields(obj["missing_counts"], "daily check_state execution_state trade_feed first_trade")
    if any(type(count) is not int or count < 0 for count in obj["missing_counts"].values()):
        raise ValueError("invalid coverage count")
    return obj


def _candle(row):
    fields(row, "symbol day source received_at response_sha256 candle")
    symbol, day = row["symbol"], _day(row["day"])
    if row["source"] != AVAILABILITY.get(symbol):
        raise ValueError("wrong daily source")
    checksum(row["response_sha256"])
    received = instant(row["received_at"])
    closed = at(day + DAY)
    if received < closed:
        raise ValueError("closed candle received before day end")
    candle = row["candle"]
    if symbol == FX:
        ts, low, high, open_, close, volume = candle
        unit = 1
        confirm = "1"
    else:
        ts, open_, high, low, close, volume, base, quote, confirm = candle
        unit = 1000
    timestamp = _epoch(ts)
    if timestamp != int(at(day).timestamp()) * unit:
        observation_day(datetime.fromtimestamp(timestamp // unit, UTC).date())
        raise ValueError("wrong daily bucket timestamp")
    if confirm == "0":
        return symbol, day, None
    if confirm != "1":
        raise ValueError("invalid confirmation state")
    if symbol != FX:
        _number(base)
        _number(volume)
        volume = quote
    low, high, open_, close, volume = map(_number, (low, high, open_, close, volume))
    if not low <= min(open_, close) <= max(open_, close) <= high:
        raise ValueError("invalid candle OHLC")
    return symbol, day, Bar(close, volume, received)


def _status(market, row):
    fields(row, "kind day symbol source start end state response_sha256")
    day, symbol = _day(row["day"], execution=True), row["symbol"]
    start, end = instant(row["start"]), instant(row["end"])
    state = row["state"]
    checksum(row["response_sha256"])
    key = day, symbol
    if row["kind"] == "feed":
        if symbol not in SYMBOLS or row["source"] != TRADE_SOURCE:
            raise ValueError("wrong trade coverage source")
        _opaque_id(state)  # stream identifier linked to the custodian's journal
        store = market.feeds
    else:
        if row["source"] != STATE_SOURCE.get(symbol) or state not in STATES:
            raise ValueError("wrong status source or value")
        if row["kind"] == "check":
            if not at(day) <= start < at(day, CHECK_HOUR) or end != at(day, CHECK_HOUR):
                raise ValueError("status check was not received before 01:00 UTC")
            _record(market.checks, key, state)
            return
        if row["kind"] != "state":
            raise ValueError("unknown evidence kind")
        store = market.states
    if not at(day, EXEC_HOUR) <= start < end <= at(day, EXEC_HOUR + 1):
        raise ValueError("continuity span outside execution window")
    store.setdefault(key, []).append(Span(start, end, state))


def _trade(row):
    fields(row, "symbol day source received_at stream response_sha256 frame")
    day, symbol = _day(row["day"], execution=True), row["symbol"]
    checksum(row["response_sha256"])
    if symbol not in SYMBOLS or row["source"] != TRADE_SOURCE:
        raise ValueError("execution input must be OKX unaggregated trades-all")
    frame = row["frame"]
    fields(frame, "arg data")
    if frame["arg"] != {"channel": "trades-all", "instId": symbol} or len(frame["data"]) != 1:
        raise ValueError("unexpected OKX trade frame")
    tick = frame["data"][0]
    fields(tick, "instId tradeId px sz side source ts")
    if tick["instId"] != symbol or tick["side"] not in ("buy", "sell") or tick["source"] not in ("0", "1"):
        raise ValueError("unexpected OKX trade fields")
    milliseconds = _epoch(tick["ts"])
    timestamp = datetime.fromtimestamp(milliseconds // 1000, UTC) + timedelta(
        milliseconds=milliseconds % 1000
    )
    received = instant(row["received_at"])
    observation_day(timestamp.date(), execution=True)
    if not at(day, EXEC_HOUR) <= timestamp <= received < at(day, EXEC_HOUR + 1):
        raise ValueError("exchange or receipt timestamp outside causal execution window")
    _number(tick["sz"])
    return day, Trade(
        symbol,
        _opaque_id(tick["tradeId"]),
        timestamp,
        received,
        _number(tick["px"]),
        _opaque_id(row["stream"]),
    )


def read_evidence(root: Path, manifest):
    market = Evidence(listed=frozenset(SYMBOLS))
    body = {}
    for name, entry in manifest["files"].items():
        raw = _clean_file(root, name)
        if len(raw) != entry["bytes"] or digest(raw) != entry["sha256"]:
            raise ValueError(f"evidence checksum mismatch: {name}")
        rows = unpack(raw)
        if not isinstance(rows, list) or len(rows) != entry["records"]:
            raise ValueError(f"evidence record count mismatch: {name}")
        body[name] = rows
    provenance = set()
    for item in body["provenance.json"]:
        fields(item, "source started_at response_sha256")
        key = item["source"], instant(item["started_at"]), checksum(item["response_sha256"])
        if key in provenance:
            raise ValueError("duplicate source receipt")
        provenance.add(key)

    def require_receipt(row, time_key):
        if (row["source"], instant(row[time_key]), checksum(row["response_sha256"])) not in provenance:
            raise ValueError("observation lacks a matching source receipt")

    seen = set()
    for row in body["availability.json"]:
        symbol, day, bar = _candle(row)
        require_receipt(row, "received_at")
        if bar:
            previous = market.daily[symbol].get(day)
            if previous and previous.available == bar.available and previous != bar:
                raise ValueError("conflicting candle at the same receipt time")
            if not previous or bar.available < previous.available:
                market.daily[symbol][day] = bar
    for row in body["status.json"]:
        _status(market, row)
        require_receipt(row, "start")
    for index in (market.states, market.feeds):
        for spans in index.values():
            spans.sort(key=lambda span: span.start)
            if any(left.end > right.start for left, right in zip(spans, spans[1:], strict=False)):
                raise ValueError("overlapping continuity spans")
    for row in body["trades.json"]:
        day, trade = _trade(row)
        require_receipt(row, "received_at")
        key = trade.symbol, trade.trade_id
        if key in seen:
            raise ValueError("duplicate trade ID")
        seen.add(key)
        market.trades.setdefault((day, trade.symbol), []).append(trade)
    return market

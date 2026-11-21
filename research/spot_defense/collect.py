"""Download public, pre-lock inputs and retain exact HTTP response bodies."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .protocol import LOCK_START, SYMBOLS, WITNESS_DAY, at

OKX = "https://www.okx.com/api/v5/market/history-candles"
COINBASE = "https://api.exchange.coinbase.com/products/USDT-USD/candles"
NOTICE_SOURCES = {
    "listing_btc": "https://www.okx.com/help/fee-details",
    "listing_eth": "https://www.okx.com/en-us/help/okx-to-adjust-components-of-several-indexes-2023-05-23",
    "incident_july": "https://www.okx.com/en-gb/help/trading-service-failure-report",
    "incident_august": "https://www.okx.com/en-us/help/aug-14-2023-trading-service-failure-report",
    "incident_september": "https://www.okx.com/help/sep-13-2023-trading-service-failure-report",
    "incident_rest": "https://www.okx.com/help/mar-27-2024-rest-api-service-issues",
}
OKX_INTERVALS = {"1Dutc": 86_400_000, "1H": 3_600_000}
CUTOFF_MS = int(at(LOCK_START).timestamp()) * 1000
USER_AGENT = "curl/8.5.0"


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def download(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=20) as response:
                return response.read()
        except (OSError, TimeoutError) as exc:
            if attempt == 3 or isinstance(exc, HTTPError) and exc.code not in {429, 500, 502, 503, 504}:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def okx_rows(body: bytes, cursor: int):
    payload = json.loads(body)
    if payload["code"] != "0":
        raise ValueError(f"OKX error {payload['code']}")
    rows = payload["data"]
    if any(len(row) != 9 or int(row[0]) >= min(cursor, CUTOFF_MS) for row in rows):
        raise ValueError("OKX response violates the exclusive UTC request boundary")
    return rows


def coinbase_rows(body: bytes, upper: int):
    rows = json.loads(body)
    if not isinstance(rows, list) or any(
        len(row) != 6 or int(row[0]) > upper or int(row[0]) >= CUTOFF_MS // 1000 for row in rows
    ):
        raise ValueError("Coinbase response violates the UTC request boundary")
    return rows


def collect(root: Path):
    root.mkdir()
    raw = root / "raw"
    raw.mkdir()
    entries = []

    def capture(name, url, series, parse, **bounds):
        body = download(url)
        rows = parse(body)
        file = raw / name
        file.write_bytes(body)
        entries.append(
            {
                "file": f"raw/{name}",
                "url": url,
                "series": series,
                "retrieved_utc": datetime.now(UTC).isoformat(),
                "sha256": sha256(body),
                "bytes": len(body),
                **bounds,
            }
        )
        return rows

    lower = int(at(WITNESS_DAY).timestamp()) * 1000
    for bar, duration in OKX_INTERVALS.items():
        for symbol in SYMBOLS:
            cursor = CUTOFF_MS
            for page in range(250):
                limit = min(300, (cursor - lower + duration - 1) // duration)
                query = urlencode(dict(instId=symbol, bar=bar, after=cursor, limit=limit))
                rows = capture(
                    f"okx-{symbol}-{bar}-{page:03}.json",
                    f"{OKX}?{query}",
                    f"{symbol}:{bar}",
                    partial(okx_rows, cursor=cursor),
                    cursor=cursor,
                )
                if not rows:
                    break
                cursor = min(int(row[0]) for row in rows)
                if cursor <= lower:
                    break
                time.sleep(0.16)
            else:
                raise ValueError("OKX pagination exceeded expected bound")

    start = at(WITNESS_DAY)
    for page in range(3):
        end = min(start + timedelta(days=250), at(LOCK_START))
        upper = int(end.timestamp()) - 1
        link = urlencode(
            {
                "granularity": 86400,
                "start": start.isoformat(),
                "end": datetime.fromtimestamp(upper, UTC).isoformat(),
            }
        )
        capture(
            f"coinbase-{page}.json",
            f"{COINBASE}?{link}",
            "USDT-USD",
            partial(coinbase_rows, upper=upper),
            lower=int(start.timestamp()),
            upper=upper,
        )
        start = end
    if start != at(LOCK_START):
        raise AssertionError("Coinbase request partition did not cover configured interval")

    for name, url in NOTICE_SOURCES.items():
        capture(f"{name}.html", url, name, lambda body: None)

    manifest = {
        "schema": 1,
        "timezone": "UTC",
        "first_requested_date": WITNESS_DAY.isoformat(),
        "end_exclusive": LOCK_START.isoformat(),
        "entries": entries,
    }
    body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (root / "manifest.json").write_bytes(body)
    print(sha256(body))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new directory for raw data and manifest")
    collect(parser.parse_args().directory)

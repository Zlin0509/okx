"""M1 public data collection and deterministic, network-free snapshot verification."""

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path

DAY = 86_400_000
HOST = "https://www.okx.com"
START = "2020-07-01"
TARGET_START = "2021-01-01"
END = "2026-11-01"  # Exclusive; includes all of 2026-10-31 UTC.
# Availability probes only. This is NOT a selected historical investment universe.
PROBES = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "LINK", "AVAX", "TON", "DOT")
PATHS = {
    "/api/v5/public/instruments",
    "/api/v5/market/history-candles",
    "/api/v5/public/funding-rate-history",
    "/api/v5/public/market-data-history",
}
DEFAULT = Path(__file__).parent / "snapshots" / "m1"


def stamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp() * 1000)


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, UTC).isoformat().replace("+00:00", "Z")


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def digest(body):
    return hashlib.sha256(body).hexdigest()


def finite(value):
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError("Non-finite numeric data")
        return result
    except InvalidOperation as exc:
        raise ValueError("Invalid numeric data") from exc


def quota_response(entry, body):
    if entry.get("status") == 429:
        return True
    try:
        return str(json.loads(body).get("code")) in {"50011", "50040"}
    except (ValueError, AttributeError):
        return False


def failed_response(entry, body):
    if entry.get("status") != 200:
        return True
    try:
        return json.loads(body).get("code") != "0"
    except (ValueError, AttributeError):
        return True


def records(root):
    result = []
    for path in sorted(root.glob("*.request.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        body = path.with_name(path.name.replace(".request.json", ".body")).read_bytes()
        if len(body) != entry["bytes"] or digest(body) != entry["sha256"]:
            raise ValueError(f"Snapshot checksum mismatch: {path.name}")
        url = urllib.parse.urlsplit(entry["url"])
        if (
            entry.get("method") != "GET"
            or url.scheme != "https"
            or url.netloc != "www.okx.com"
            or url.path not in PATHS
        ):
            raise ValueError("Unexpected snapshot source")
        result.append((entry, body))
    return result


class PublicCollector:
    """Only allowlisted unauthenticated GETs; preserve failures, never retry a failed path."""

    def __init__(self, root, budget=350):
        if (root / "manifest.json").exists():
            raise ValueError("Frozen snapshot is read-only; choose a new snapshot directory")
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = {entry["url"]: (entry, body) for entry, body in records(root)}
        self.blocked = {
            urllib.parse.urlsplit(url).path
            for url, (entry, body) in self.cache.items()
            if failed_response(entry, body)
        }
        self.quota_stopped = any(quota_response(entry, body) for entry, body in self.cache.values())
        self.remaining = budget

    def get(self, path, params):
        if path not in PATHS:
            raise ValueError("Endpoint outside public research allowlist")
        url = HOST + path + "?" + urllib.parse.urlencode(params)
        if self.quota_stopped:
            raise RuntimeError("Quota stopped; no further provider requests permitted")
        if url in self.cache:
            entry, body = self.cache[url]
        else:
            if path in self.blocked or self.remaining <= 0:
                raise RuntimeError("Endpoint blocked or request budget exhausted; no retries")
            self.remaining -= 1
            time.sleep(0.5)
            entry = {"url": url, "method": "GET", "retrieved_at": datetime.now(UTC).isoformat()}
            body = b""
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "OKX-offline-research/1.0"})
                # No redirect to another host, credentials, application imports, or .env loading.
                opener = urllib.request.build_opener(NoRedirect())
                with opener.open(request, timeout=25) as response:
                    body = response.read()
                    entry["status"] = response.status
            except urllib.error.HTTPError as exc:
                entry["status"] = exc.code
                body = exc.read()
            except (OSError, urllib.error.URLError) as exc:
                entry["error_type"] = type(exc).__name__
            entry.update(bytes=len(body), sha256=digest(body))
            name = digest(url.encode())[:24]
            (self.root / f"{name}.body").write_bytes(body)
            dump(self.root / f"{name}.request.json", entry)
            self.cache[url] = (entry, body)
        if entry.get("status") != 200:
            self.blocked.add(path)
            self.quota_stopped |= entry.get("status") == 429
            raise RuntimeError(
                f"Public data request stopped: HTTP {entry.get('status', 'transport failure')}"
            )
        payload = json.loads(body)
        if payload.get("code") != "0":
            self.blocked.add(path)
            self.quota_stopped |= str(payload.get("code")) in {"50011", "50040"}
            raise RuntimeError(f"Public data request stopped: OKX {payload.get('code')}")
        if not isinstance(payload.get("data"), list):
            raise ValueError("Invalid public data response")
        return payload["data"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def candle_summary(rows, start, end):
    by_time = {}
    duplicate_rows = 0
    for row in rows:
        if len(row) != 9 or row[8] != "1":
            raise ValueError("Incomplete or malformed historical daily candle")
        ts = int(row[0])
        values = [finite(v) for v in row[1:8]]
        if ts % DAY or not all(v.is_finite() for v in values):
            raise ValueError("Non-UTC or non-finite daily candle")
        o, h, low, c, *volumes = values
        if min(o, h, low, c) <= 0 or low > min(o, c) or h < max(o, c) or any(v < 0 for v in volumes):
            raise ValueError("Invalid OHLC or volume")
        if ts in by_time:
            if by_time[ts] != row:
                raise ValueError("Conflicting duplicate candle")
            duplicate_rows += 1
        by_time[ts] = row
    timestamps = sorted(t for t in by_time if start <= t < end)
    missing = sorted(set(range(start, end, DAY)) - set(timestamps))
    internal = [t for t in missing if timestamps and timestamps[0] < t < timestamps[-1]]
    return {
        "rows": len(timestamps),
        "first": iso(timestamps[0]) if timestamps else None,
        "last": iso(timestamps[-1]) if timestamps else None,
        "expected_calendar_days": len(range(start, end, DAY)),
        "missing_calendar_days": len(missing),
        "internal_missing_dates": [iso(t) for t in internal],
        "duplicate_rows_across_probes": duplicate_rows,
    }


def common_summary(groups, step):
    common = sorted(set.intersection(*groups)) if groups else []
    present = set(common)
    return {
        "rows": len(common),
        "first": iso(common[0]) if common else None,
        "last": iso(common[-1]) if common else None,
        "missing_in_common_span": [iso(t) for t in range(common[0], common[-1] + 1, step) if t not in present]
        if common
        else [],
    }


def candidate_coverage(candles, funding):
    available = sorted(set(candles) & set(funding))
    definitions = [("pair", list(pair)) for pair in combinations(available, 2)]
    core = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    if set(core) <= set(available):
        definitions += [("basket", [symbol, *core]) for symbol in available if symbol not in core]
    result = []
    for kind, legs in definitions:
        price_sets = [
            {int(r[0]) for r in candles[s] if stamp(TARGET_START) <= int(r[0]) < stamp(END)} for s in legs
        ]
        fund_sets = [{t for t in funding[s] if stamp(TARGET_START) <= t < stamp(END)} for s in legs]
        result.append(
            {
                "kind": kind,
                "legs": legs,
                "status": "availability candidate only; not tested or eligible for trading",
                "target_common_daily_candles": common_summary(price_sets, DAY),
                "training_common_days": len(
                    {t for t in set.intersection(*price_sets) if t < stamp("2025-01-01")}
                ),
                "target_common_funding_settlements": common_summary(fund_sets, 8 * 3_600_000),
            }
        )
    return result


def audit(root):
    entries = records(root)
    candles = {}
    funding = {}
    instruments = []
    failures = []
    empty_requests = []
    for entry, body in entries:
        url = urllib.parse.urlsplit(entry["url"])
        params = dict(urllib.parse.parse_qsl(url.query))
        if entry.get("status") != 200:
            failures.append({"url": entry["url"], "status": entry.get("status"), "sha256": entry["sha256"]})
            continue
        payload = json.loads(body)
        if payload.get("code") != "0":
            failures.append({"url": entry["url"], "code": payload.get("code")})
            continue
        rows = payload["data"]
        if not rows:
            empty_requests.append(entry["url"])
        if url.path.endswith("/instruments"):
            instruments.extend(rows)
        elif url.path.endswith("/history-candles"):
            if params.get("bar") != "1Dutc":
                raise ValueError("Expected UTC daily candles")
            candles.setdefault(params["instId"], []).extend(rows)
        elif url.path.endswith("/funding-rate-history"):
            group = funding.setdefault(params["instId"], {})
            for row in rows:
                if row["instId"] != params["instId"]:
                    raise ValueError("Invalid realized funding rate or instrument")
                finite(row["realizedRate"])
                ts = int(row["fundingTime"])
                if ts in group and group[ts] != row:
                    raise ValueError("Conflicting duplicate funding record")
                group[ts] = row
    linear = [r for r in instruments if r.get("settleCcy") == "USDT" and r.get("ctType") == "linear"]
    candle_results = {}
    for symbol, rows in sorted(candles.items()):
        meta = next((r for r in linear if r["instId"] == symbol), {})
        candle_results[symbol] = {
            "warmup_and_target": candle_summary(rows, stamp(START), stamp(END)),
            "target": candle_summary(rows, stamp(TARGET_START), stamp(END)),
            "current_metadata_list_time": iso(int(meta["listTime"])) if meta.get("listTime") else None,
        }
    funding_results = {}
    for symbol, group in sorted(funding.items()):
        times = sorted(group)
        target = [t for t in times if stamp(TARGET_START) <= t < stamp(END)]
        funding_results[symbol] = {
            "rows": len(times),
            "first": iso(times[0]) if times else None,
            "last": iso(times[-1]) if times else None,
            "target_rows": len(target),
            "target_first": iso(target[0]) if target else None,
            "target_last": iso(target[-1]) if target else None,
            "observed_intervals_hours": sorted(
                {(b - a) / 3_600_000 for a, b in zip(times, times[1:], strict=False)}
            ),
            "long_history_complete": False,
        }
    candle_sets = [
        {int(r[0]) for r in rows if stamp(TARGET_START) <= int(r[0]) < stamp(END)}
        for _, rows in sorted(candles.items())
    ]
    funding_sets = [
        {t for t in group if stamp(TARGET_START) <= t < stamp(END)} for _, group in sorted(funding.items())
    ]
    common_funding = set.intersection(*funding_sets) if funding_sets else set()
    full_funding_days = {
        t // DAY * DAY
        for t in common_funding
        if all(t // DAY * DAY + hour * 3_600_000 in common_funding for hour in (0, 8, 16))
    }
    return {
        "schema_version": 1,
        "target_utc": {"start_inclusive": TARGET_START, "end_exclusive": END, "warmup_start": START},
        "base_commit": "d67cfd2",
        "purpose": "M1 availability audit only; no strategy, selected universe, or performance results",
        "snapshot_requests": len(entries),
        "raw_bytes": sum(entry["bytes"] for entry, _ in entries),
        "retrieved_first": min((e["retrieved_at"] for e, _ in entries), default=None),
        "retrieved_last": max((e["retrieved_at"] for e, _ in entries), default=None),
        "current_instruments": {
            "all_swap": len(instruments),
            "usdt_linear": len(linear),
            "states": sorted({r["state"] for r in linear}),
        },
        "historical_universe_complete": False,
        "delisted_metadata_restored": False,
        "candles": candle_results,
        "funding": funding_results,
        "candidate_coverage": candidate_coverage(candles, funding),
        "cross_asset_alignment": {
            "scope": "successfully captured instruments only; TON and DOT excluded, not silently filled",
            "candle_instruments": sorted(candles),
            "funding_instruments": sorted(funding),
            "common_target_daily_candles": common_summary(candle_sets, DAY),
            "common_target_funding_settlements": common_summary(funding_sets, 8 * 3_600_000),
            "complete_common_funding_days_on_observed_8h_grid": common_summary([full_funding_days], DAY),
            "funding_grid_boundary": "8h is observed in this snapshot only, not a historical schedule assumption",
        },
        "failed_requests": failures,
        "empty_requests": sorted(empty_requests),
        "ready_for_full_period_costed_backtest": False,
    }


def collect(root):
    client = PublicCollector(root)
    client.get("/api/v5/public/instruments", {"instType": "SWAP"})
    for coin in PROBES:
        symbol = coin + "-USDT-SWAP"
        cursor = stamp(END)
        for _ in range(30):
            rows = client.get(
                "/api/v5/market/history-candles",
                {
                    "instId": symbol,
                    "bar": "1Dutc",
                    "after": str(cursor),
                    "limit": "100",
                },
            )
            if not rows:
                break
            oldest = min(int(row[0]) for row in rows)
            if any(int(row[0]) >= cursor for row in rows) or oldest >= cursor:
                raise ValueError("Historical pagination made no strict backward progress")
            if oldest <= stamp(START):
                break
            cursor = oldest
        else:
            raise RuntimeError("Candle page budget exhausted")
        funding_rows = client.get("/api/v5/public/funding-rate-history", {"instId": symbol, "limit": "400"})
        if len(funding_rows) >= 400:
            raise RuntimeError(
                "Funding page may be truncated; completeness refused, no automatic provider retry"
            )
        print(f"Captured {symbol}", flush=True)


def freeze(root):
    if (root / "manifest.json").exists():
        raise ValueError("Frozen snapshot is read-only; choose a new snapshot directory")
    dump(root / "coverage.json", audit(root))
    files = sorted(p for p in root.iterdir() if p.is_file() and p.name != "manifest.json")
    dump(root / "manifest.json", {p.name: digest(p.read_bytes()) for p in files})


def verify(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    actual = {
        p.name: digest(p.read_bytes()) for p in root.iterdir() if p.is_file() and p.name != "manifest.json"
    }
    if actual != manifest:
        raise ValueError("Manifest mismatch: modified, missing, or unregistered snapshot file")
    result = audit(root)
    if result != json.loads((root / "coverage.json").read_text(encoding="utf-8")):
        raise ValueError("Coverage does not reproduce")
    print(f"Verified {result['snapshot_requests']} requests; coverage reproduces; full backtest ready=False")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "verify", "freeze"))
    parser.add_argument("--snapshot", type=Path, default=DEFAULT)
    args = parser.parse_args()
    if args.action in {"collect", "freeze"} and (args.snapshot / "manifest.json").exists():
        raise ValueError("Frozen snapshot is read-only; choose a new snapshot directory")
    if args.action == "collect":
        try:
            collect(args.snapshot)
        finally:
            freeze(args.snapshot)
    elif args.action == "freeze":
        freeze(args.snapshot)
    else:
        verify(args.snapshot)


if __name__ == "__main__":
    main()

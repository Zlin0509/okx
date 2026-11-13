"""Daily training eligibility: verified dates and provisional metadata diagnostics are distinct."""

import csv
import gzip
import io
import json
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .data_audit import DAY, DEFAULT, digest, iso, records, stamp
from .execution_audit import run_logged

TURNOVER_FLOOR = Decimal("10000000")


def liquidity_window(day, turnover, minimum=TURNOVER_FLOOR):
    """Exact 30 preceding UTC days; neither today's nor future volume enters eligibility."""
    values, reasons = [], []
    for lag in range(1, 31):
        raw = turnover.get(day - lag * DAY)
        if raw is None or raw == "":
            reasons.append("missing_lagged_turnover_value")
            continue
        try:
            value = Decimal(str(raw))
            if not value.is_finite() or value < 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            reasons.append("invalid_lagged_turnover_value")
            continue
        values.append(value)
    with localcontext() as context:
        context.prec = 34
        mean = sum(values, Decimal(0)) / 30 if len(values) == 30 else None
    if mean is not None and mean < minimum:
        reasons.append("below_lagged_turnover_threshold")
    return {
        "mean_daily_usdt": format(mean, "f") if mean is not None else None,
        "valid_observations": len(values),
        "minimum_mean_daily_usdt": str(minimum),
        "passed": not reasons,
        "reasons": sorted(set(reasons)),
    }


def leg_reasons(day, prices, listing, continuous, verified, age=180, lookback=56, turnover=None):
    unknown = []
    reasons = []
    if not verified:
        unknown.append("historical_tradability_unverified")
    if listing is None:
        unknown.append("listing_time_unknown")
    if continuous is None:
        unknown.append("continuous_trading_time_unknown")
    if listing is not None and continuous is not None:
        first = ((max(listing, continuous) + DAY - 1) // DAY) * DAY
        admitted = first + age * DAY
        if day < first:
            reasons.append("before_complete_trading_day")
        if day < admitted:
            reasons.append("listing_age_180_not_met")
        if day < admitted + lookback * DAY:
            reasons.append("rolling_post_admission_warmup")
    if day not in prices:
        reasons.append("missing_current_complete_candle")
    if any(day - lag * DAY not in prices for lag in range(1, lookback + 1)):
        reasons.append("missing_lagged_price_window")
    if any(day - lag * DAY not in prices for lag in range(1, 31)):
        reasons.append("missing_lagged_turnover_window")
    reasons.extend(liquidity_window(day, turnover or {})["reasons"])
    return sorted(set(unknown)), sorted(set(reasons))


def evaluate_candidate(legs, prices, metadata, dates, age=180, lookback=56, turnover=None):
    rows = []
    for day in dates:
        unknown, reasons = [], []
        for symbol in legs:
            meta = metadata.get(symbol, {})
            missing, excluded = leg_reasons(
                day,
                prices.get(symbol, set()),
                meta.get("listing"),
                meta.get("continuous"),
                meta.get("verified", False),
                age,
                lookback,
                (turnover or {}).get(symbol, {}),
            )
            unknown.extend(symbol + ":" + reason for reason in missing)
            reasons.extend(symbol + ":" + reason for reason in excluded)
        rows.append(
            {
                "date": iso(day)[:10],
                "verified_status": "unknown" if unknown else "excluded" if reasons else "eligible",
                "provisional_current_metadata_pass": not reasons
                and not any("time_unknown" in r for r in unknown),
                "unknown_reasons": unknown,
                "provisional_exclusion_reasons": reasons,
                "liquidity_by_leg": {s: liquidity_window(day, (turnover or {}).get(s, {})) for s in legs},
            }
        )
    return rows


def add_candles(rows, prices, turnover, symbol):
    for row in rows:
        if row[8] != "1":
            continue
        ts = int(row[0])
        prices.setdefault(symbol, set()).add(ts)
        group = turnover.setdefault(symbol, {})
        if ts in group and group[ts] != row[7]:
            raise ValueError("Conflicting duplicate volCcyQuote")
        group[ts] = row[7]


def render(root=DEFAULT):
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    prices, metadata, turnover = {}, {}, {}
    provenance = []
    rate_pages = []
    for entry, body in records(root):
        url = urlsplit(entry["url"])
        params = dict(parse_qsl(url.query))
        name = next(
            p.name
            for p in root.glob("*.request.json")
            if json.loads(p.read_text(encoding="utf-8"))["url"] == entry["url"]
        )
        named = name.startswith(("initial-", "btc-"))
        provenance.append(
            {
                "request_file": name,
                "url": entry["url"],
                "acquisition": "one_off_urllib_probe" if named else "M1_collector",
                "original_client_hash_recorded": False,
                "headers_recorded": False,
            }
        )
        if entry.get("status") != 200:
            continue
        payload = json.loads(body)
        if payload.get("code") != "0":
            continue
        if url.path.endswith("/instruments"):
            for row in payload["data"]:
                metadata[row["instId"]] = {
                    "listing": int(row["listTime"]) if row.get("listTime") else None,
                    "continuous": int(row["contTdSwTime"]) if row.get("contTdSwTime") else None,
                    "verified": False,
                    "source": "current instrument metadata only; not historical evidence",
                }
        elif url.path.endswith("/history-candles"):
            add_candles(payload["data"], prices, turnover, params["instId"])
        elif url.path.endswith("/funding-rate-history"):
            rate_pages.append(
                {
                    "request_file": name,
                    "instrument": params["instId"],
                    "returned_rows": len(payload["data"]),
                    "limit": int(params["limit"]),
                    "full_page_may_be_truncated": len(payload["data"]) >= int(params["limit"]),
                }
            )
    dates = tuple(range(stamp("2021-01-01"), stamp("2025-01-01"), DAY))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        [
            "candidate",
            "kind",
            "date",
            "verified_status",
            "provisional_current_metadata_pass",
            "unknown_reasons",
            "provisional_exclusion_reasons",
            "liquidity_by_leg",
        ]
    )
    candidates = []
    for candidate in coverage["candidate_coverage"]:
        legs = candidate["legs"]
        label = candidate["kind"] + ":" + "/".join(legs)
        rows = evaluate_candidate(legs, prices, metadata, dates, turnover=turnover)
        reason_counts = Counter()
        for row in rows:
            reason_counts.update(row["unknown_reasons"] + row["provisional_exclusion_reasons"])
            writer.writerow(
                [
                    label,
                    candidate["kind"],
                    row["date"],
                    row["verified_status"],
                    row["provisional_current_metadata_pass"],
                    "|".join(row["unknown_reasons"]),
                    "|".join(row["provisional_exclusion_reasons"]),
                    json.dumps(row["liquidity_by_leg"], sort_keys=True),
                ]
            )
        unknown = any(row["verified_status"] == "unknown" for row in rows)
        provisional = [row["date"] for row in rows if row["provisional_current_metadata_pass"]]
        candidates.append(
            {
                "candidate": label,
                "legs": legs,
                "raw_training_intersection_days_NOT_effective": candidate["training_common_days"],
                "verified_eligible_count": None
                if unknown
                else sum(row["verified_status"] == "eligible" for row in rows),
                "verified_eligible_dates": None
                if unknown
                else [row["date"] for row in rows if row["verified_status"] == "eligible"],
                "provisional_current_metadata_pass_count_NOT_eligible": len(provisional),
                "provisional_current_metadata_pass_dates_NOT_eligible": provisional,
                "exclusion_and_unknown_reason_counts": dict(sorted(reason_counts.items())),
                "statistical_testing_qualified": False,
                "qualification_judgment": "fail-closed: historical tradability/universe not verified; do not enter FDR",
            }
        )
    summary = {
        "revision": 3,
        "liquidity_rule": {
            "field": "volCcyQuote row[7] in USDT",
            "window": "[t-30d,t)",
            "minimum_mean_daily_usdt": str(TURNOVER_FLOOR),
            "required_observations": 30,
            "not_a_historical_ranking_or_capacity_estimate": True,
        },
        "original_snapshot_unchanged": True,
        "training_calendar_days": len(dates),
        "candidates": candidates,
        "listing_metadata": {
            s: metadata.get(s, {"listing": None, "continuous": None, "verified": False})
            for s in sorted(prices)
        },
        "original_false_flags": {
            "kind": "manual fail-closed audit judgments, not derived completeness tests",
            "fields": [
                "long_history_complete",
                "historical_universe_complete",
                "delisted_metadata_restored",
                "ready_for_full_period_costed_backtest",
            ],
        },
        "funding_page_checks": rate_pages,
        "funding_start_difference": "BTC 277 rows starts Aug13 08:00; others 285 start Aug10 16:00. Provider cause unknown; observed minima are not guaranteed history floors. No retry.",
        "early_funding_evidence": "after=2021-02-01 returns empty before that boundary only; no inference for all of 2021; archive request denied HTTP403/1010",
        "acquisition_provenance": provenance,
        "acquisition_limit": "197 requests are verifiable observations; not a claim that collect alone recreates the original probes end to end",
        "new_collector_policy": "frozen directory refused; a funding page at limit is an explicit truncation error",
    }
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0, compresslevel=9) as handle:
        handle.write(stream.getvalue().encode())
    return {
        "eligibility_review_r2.json": (
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode(),
        "eligibility_daily.csv.gz": compressed.getvalue(),
    }


def write_results():
    output = Path(__file__).parent / "results/m2_review_r2"
    output.mkdir(parents=True, exist_ok=True)
    rendered = render()
    for name, content in rendered.items():
        (output / name).write_bytes(content)
    return {name: digest(content) for name, content in rendered.items()}


def main():
    run_logged("training-liquidity-r2", write_results)


if __name__ == "__main__":
    main()

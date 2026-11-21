"""Produce deterministic training/validation diagnostics and synthetic stress results offline."""

import argparse
import json
from pathlib import Path

from .data import FX, load
from .engine import metrics, series_metrics, simulate
from .protocol import COST_BPS, DAY, LOCK_END, LOCK_START, SYMBOLS, WARMUP_START, WINDOWS, at, days
from .stress import run_stress


def audit_coverage(market, start, end):
    daily = {
        symbol: [day.isoformat() for day in days(start, end) if not market.bar(symbol, day, at(day + DAY))]
        for symbol in (*SYMBOLS, FX)
    }
    execution = {
        symbol: [day.isoformat() for day in days(start, end) if market.execution_price(symbol, day) is None]
        for symbol in SYMBOLS
    }
    return {"days": (end - start).days, "missing_daily": daily, "missing_execution_open": execution}


def cash_benchmarks(ledger, initial_usd):
    close_fx = [row["fx_at_close"] for row in ledger]
    initial_fx = ledger[0]["fx_at_check"] if initial_usd is not None else None
    usd_cash = series_metrics([1.0] * (len(ledger) + 1))
    stable_cash = series_metrics([initial_fx, *close_fx])
    return {
        "usd_cash": {
            "usd": usd_cash,
            "usdt_return": initial_fx / close_fx[-1] - 1 if initial_fx and close_fx[-1] else None,
            "annualized_two_way_turnover": 0.0,
            "max_post_total_exposure_measured": 0.0,
        },
        "usdt_cash": {
            "usd": stable_cash,
            "usdt_return": 0.0,
            "annualized_two_way_turnover": 0.0,
            "max_post_total_exposure_measured": 0.0,
        },
    }


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run(data, manifest_sha256, output):
    market, evidence = load(data, manifest_sha256)
    stress = run_stress()
    output.mkdir()
    write_json(output / "stress.json", stress)
    summary = {
        "evidence": evidence,
        "warmup_coverage": audit_coverage(market, WARMUP_START, WINDOWS["train"][0]),
        "locked_out_of_sample": {
            "start": str(LOCK_START),
            "inclusive_end": str(LOCK_END),
            "status": "sealed",
        },
        "stress_protections_passed": all(all(item["protections"].values()) for item in stress.values()),
        "windows": {},
        "research_conclusion": "retain_cash_due_to_insufficient_evidence",
    }
    for name, (start, end) in WINDOWS.items():
        result = {
            "start": str(start),
            "inclusive_end": str(end - DAY),
            "coverage": audit_coverage(market, start, end),
            "by_cost_bp": {},
        }
        for bp in COST_BPS:
            strategy, initial_usd = simulate(market, start, end, bp)
            passive, passive_base = simulate(market, start, end, bp, passive=True)
            write_json(output / f"{name}-{bp}bp-ledgers.json", {"strategy": strategy, "passive": passive})
            result["by_cost_bp"][bp] = {
                "defense": metrics(strategy, initial_usd, assess=name == "validation"),
                "passive_btc_eth": metrics(passive, passive_base),
                **cash_benchmarks(strategy, initial_usd),
            }
        summary["windows"][name] = result
    write_json(output / "summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("manifest_sha256")
    parser.add_argument("output", type=Path, help="new directory for results")
    config = parser.parse_args()
    run(config.data, config.manifest_sha256, config.output)

"""Reproduce M2 accounting diagnostics and the strict M1 short-window data gate offline."""

import argparse
import csv
import io
import json
import sys
from dataclasses import replace
from pathlib import Path

from .data_audit import DEFAULT, digest, verify
from .engine import (
    ZERO,
    Costs,
    Engine,
    Frame,
    Leg,
    Market,
    Rejected,
    Signal,
    number,
    serializable,
)
from .execution_audit import run_logged

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "research/fixtures/m2_synthetic.json"
RESULTS = ROOT / "research/results/m2_r2"
REAL_CHECK = ROOT / "research/audit/m2_real_check.json"
FREEZE = ROOT / "research/M2_FREEZE_R2.json"


def synthetic_inputs():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    market = Market(
        tuple(Frame(f["ts"], f["marks"], f["execution"]) for f in raw["frames"]),
        tuple(raw["frame_calendar"]),
        {s: tuple(t) for s, t in raw["execution_calendar"].items()},
        {s: tuple(t) for s, t in raw["funding_calendar"].items()},
        {(r["ts"], r["symbol"]): number(r["realized_rate"]) for r in raw["funding"]},
        raw["provenance"],
        raw["calendar_source"],
    )
    signals = tuple(
        Signal(
            s["id"], s["known_at"], {Leg(t["book"], t["symbol"]): number(t["quantity"]) for t in s["targets"]}
        )
        for s in raw["signals"]
    )
    return raw, market, signals


def synthetic_diagnostics():
    raw, market, signals = synthetic_inputs()
    baseline = Costs(number(raw["costs"]["fee_bps"]), number(raw["costs"]["slippage_bps"]))
    delayed = replace(signals[0], delay_steps={Leg("pair", "B"): 1})
    partial = replace(signals[0], fill_fraction={Leg("pair", "B"): number("0.5")})
    missing_leg = replace(signals[0], fill_fraction={Leg("pair", "B"): ZERO})
    cases = {
        "hand_calculated": (signals, baseline),
        "zero_cost": (signals, Costs(ZERO, ZERO)),
        "double_fee_slippage": (signals, Costs(baseline.fee_bps * 2, baseline.slippage_bps * 2)),
        "second_leg_delayed": ((delayed, signals[1]), baseline),
        "second_leg_partial": ((partial, signals[1]), baseline),
        "second_leg_unfilled": ((missing_leg, signals[1]), baseline),
    }
    runs = {}
    for name, (orders, costs) in cases.items():
        result = Engine(market, raw["initial_equity"], costs).run(orders)
        if abs(result["reconciliation_error"]) > number("1e-24"):
            raise AssertionError("Ledger does not reconcile")
        runs[name] = serializable(result)
    for key, value in raw["hand_calculated_expected"].items():
        if number(runs["hand_calculated"][key]) != number(value):
            raise AssertionError(f"Independent hand calculation disagrees: {key}")
    return {
        "label": raw["label"],
        "performance_or_strategy_evidence": False,
        "costs_are_test_assumptions_not_account_fee_quotes": True,
        "runs": runs,
    }


def real_window_inputs(root=DEFAULT):
    raise Rejected(
        "real_window_disabled", "M2 R2 forbids real-window execution; use preserved cached evidence only"
    )


def real_integration():
    raise Rejected(
        "real_window_disabled", "M2 R2 forbids real-window execution; use preserved cached evidence only"
    )


def frozen_sources():
    spec = json.loads(FREEZE.read_text(encoding="utf-8"))
    for path, sha in spec["source_sha256"].items():
        if digest((ROOT / path).read_bytes()) != sha:
            raise Rejected("freeze_mismatch", f"Changed source after preregistration: {path}")
    return spec


def require_controlled_snapshot():
    if not (DEFAULT / "manifest.json").exists() or not FREEZE.exists():
        raise Rejected(
            "controlled_snapshot_required",
            "This entry requires an authorized controlled snapshot; use m2 unit for code-only verification",
        )


def integrate_once():
    raise Rejected(
        "real_window_disabled", "M2 R2 forbids real-window execution; use preserved cached evidence only"
    )


def cached_real_result():
    frozen_sources()
    record = json.loads(REAL_CHECK.read_text(encoding="utf-8"))
    if record["status"] != "complete" or record["freeze_sha256"] != digest(
        (ROOT / "research/M2_FREEZE.json").read_bytes()
    ):
        raise Rejected("freeze_mismatch", "Real check is incomplete or belongs to a different freeze")
    return record["result"]


def render_results():
    synthetic = synthetic_diagnostics()
    real = cached_real_result()  # Verify recorded evidence; never rerun the locked real window.
    output = {
        "accounting.json": json.dumps(synthetic, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "real_79d_integration.json": json.dumps(real, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }
    fields = [
        "scenario",
        "seq",
        "ts",
        "event",
        "book",
        "symbol",
        "known_at",
        "requested_delta",
        "executed_delta",
        "reference_price",
        "execution_price",
        "fee",
        "cash_delta",
        "rate",
        "settlement_mark",
        "realized_delta",
        "cash",
        "equity",
        "realized_pnl",
        "unrealized_pnl",
        "cumulative_fees",
        "cumulative_funding_cashflow",
        "cumulative_slippage_diagnostic",
        "gross_notional",
        "net_notional",
        "single_leg_books",
        "asset_absolute_notional",
        "positions",
        "remaining_target_quantity",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for name, run in synthetic["runs"].items():
        for row in run["ledger"]:
            writer.writerow(
                {
                    "scenario": name,
                    **{
                        key: json.dumps(value, sort_keys=True) if isinstance(value, (list, dict)) else value
                        for key, value in row.items()
                    },
                }
            )
    output["ledger.csv"] = stream.getvalue()
    files = [
        "research/M2_PROTOCOL_R2.json",
        "research/M2_FREEZE_R2.json",
        "research/audit/m2_real_check.json",
        "research/engine.py",
        "research/m2.py",
        "research/fixtures/m2_synthetic.json",
        "research/snapshots/m1/manifest.json",
    ]
    hashes = {path: digest((ROOT / path).read_bytes()) for path in files}
    hashes.update({"research/results/m2_r2/" + name: digest(body.encode()) for name, body in output.items()})
    output["manifest.json"] = json.dumps(hashes, indent=2, sort_keys=True) + "\n"
    return output


def perform(action):
    if action == "unit":
        result = synthetic_diagnostics()
        print("M2: 6 synthetic diagnostic ledgers reconcile; no real data evaluated")
        return {"accounting_sha256": digest(json.dumps(result, sort_keys=True).encode())}
    if action == "integrate-real-once":
        integrate_once()
    require_controlled_snapshot()
    verify(DEFAULT)  # Original M1 dataset is immutable; no network or credential loading.
    output = render_results()
    if action == "write":
        RESULTS.mkdir(parents=True, exist_ok=True)
        for name, content in output.items():
            (RESULTS / name).write_text(content, encoding="utf-8")
    else:
        if {p.name for p in RESULTS.iterdir() if p.is_file()} != set(output):
            raise Rejected("artifact_mismatch", "Missing or unregistered M2 result file")
        for name, content in output.items():
            if (RESULTS / name).read_text(encoding="utf-8") != content:
                raise Rejected("artifact_mismatch", f"M2 result does not reproduce: {name}")
    print("M2 R2: synthetic ledgers reproduce; legacy cached values verified, historical run counts unproven")
    return {"manifest_sha256": digest(output["manifest.json"].encode()), "real_window_runs_this_operation": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("unit", "integrate-real-once", "write", "verify"))
    args = parser.parse_args()
    try:
        run_logged("m2-" + args.action, lambda: perform(args.action))
    except Rejected as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

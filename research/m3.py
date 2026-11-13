"""One-command, standard-library synthetic M3 reproduction. Real data are forbidden."""

import argparse
import csv
import hashlib
import io
import json
import math
import random
import sys
from pathlib import Path

from .causal import CausalEngine
from .engine import Costs, Frame, Market, Rejected, number, serializable
from .m3_audit import audited
from .portfolio import DAY, Portfolio, Relation, holding_periods
from .risk_trace import exposure_trace
from .spreads import Observation, Qualification
from .synthetic_guard import GUARD_SCOPE, install

ROOT = Path(__file__).resolve().parent.parent
PARAMS = ROOT / "research/M3_PROTOCOL_R2.json"
RESULTS = ROOT / "research/results/m3_r2_v2"
FREEZE = ROOT / "research/M3B_SOURCE_FREEZE_V2.json"
CASES = (
    "convergence",
    "nonconvergence",
    "relationship_break",
    "missing",
    "zero_volatility",
    "unknown_qualification",
    "shared_asset",
    "basket",
    "delayed_leg",
    "partial_leg",
    "unfilled_leg",
    "hard_drift",
)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def config():
    return json.loads(PARAMS.read_text(encoding="utf-8"))


def inputs(case):
    if case not in CASES:
        raise Rejected("invalid_case", "Choose a registered synthetic case")
    p = config()
    rng = random.Random(p["seed"])
    closes = []
    b, d = math.log(50), math.log(30)
    for day in range(p["days"]):
        b += rng.gauss(0, 0.012)
        d += rng.gauss(0, 0.014)
        residual = rng.gauss(0, 0.004)
        other = rng.gauss(0, 0.004)
        if day == 56:
            residual, other = 0.011, 0.011
        if day == 57:
            residual, other = 0.007, 0.007
        if day == 58:
            residual, other = 0.0001, 0.0001
        if case == "nonconvergence" and 56 <= day <= 90:
            residual = 0.011
        if case == "relationship_break" and day >= 57:
            residual = 0.011 if day < 60 else 0.06
        if case == "missing" and day == 57:
            residual = 0.011
        if case == "zero_volatility":
            residual = 0.0
        prices = {
            "A": math.exp(0.4 + 1.2 * b + residual),
            "B": math.exp(b),
            "C": math.exp(1.0 + 0.8 * b + 0.3 * d + other),
            "D": math.exp(d),
        }
        if case == "hard_drift" and day >= 57:
            prices["A"] *= 2.0
        closes.append({s: number(format(value, ".16g")) for s, value in prices.items()})
    frames, observations = [], {}
    for day, prices in enumerate(closes):
        ts = day * DAY
        opening = closes[day - 1] if day else prices
        frames.extend((Frame(ts, opening, opening), Frame(ts + DAY - 1, prices, {})))
        obs = dict(prices)
        if case == "missing" and day == 57:
            del obs["A"]
        observations[ts + DAY - 1] = Observation(ts + DAY - 1, ts + DAY - 1, obs)
    opens = tuple(day * DAY for day in range(p["days"]))
    symbols = tuple(sorted(closes[0]))
    market = Market(
        tuple(frames),
        tuple(f.ts for f in frames),
        {s: opens for s in symbols},
        {s: opens for s in symbols},
        {
            (ts, s): number("0.0001") * (1 if (ts // DAY + i) % 2 else -1)
            for ts in opens
            for i, s in enumerate(symbols)
        },
        "synthetic_test",
        "SYNTHETIC daily funding schedule, not an exchange calendar",
    )
    definitions = [("pair", ("A", "B"))]
    if case == "shared_asset":
        definitions += [("second", ("C", "B", "D"))]
    if case == "basket":
        definitions = [("basket", ("C", "B", "D"))]
    relations = tuple(
        Relation(
            book,
            syms,
            Qualification(
                syms,
                55 * DAY + DAY - 1,
                "unknown" if case == "unknown_qualification" else "qualified",
                "synthetic_fixture_truth",
            ),
        )
        for book, syms in definitions
    )
    stress = {}
    if case == "delayed_leg":
        stress = {"pair": {"delay_steps": {"B": 2}}}
    if case in {"partial_leg", "unfilled_leg"}:
        stress = {"pair": {"fill_fraction": {"B": "0.5" if case == "partial_leg" else "0"}}}
    return market, observations, relations, stress


def run_case(case, supplied=None):
    market, observations, relations, stress = supplied or inputs(case)
    p = config()
    portfolio = Portfolio(relations, p, stress)
    engine = CausalEngine(market, costs=Costs(number(p["fee_bps"]), number(p["slippage_bps"])))
    result = engine.run_causal(portfolio, observations)
    if abs(result["reconciliation_error"]) > number("1e-24"):
        raise AssertionError("Synthetic ledger does not reconcile")
    return serializable(
        {
            "label": "SYNTHETIC TEST ONLY — no strategy or statistical evidence",
            "case": case,
            "performance_evidence": False,
            "market_beta": None,
            "market_beta_reason": "not_estimated_in_M3",
            "signals": portfolio.events,
            "holdings": holding_periods(result["ledger"]),
            "risk_exposure": exposure_trace(result["ledger"]),
            "accounting": result,
        }
    )


def assert_behaviors(runs):
    expected = {
        "convergence": "mean_reversion",
        "nonconvergence": "timeout",
        "relationship_break": "relationship_failure",
        "missing": "missing_observation",
        "delayed_leg": "hedge_failure",
        "partial_leg": "hedge_failure",
        "unfilled_leg": "hedge_failure",
    }
    for case, reason in expected.items():
        if not any(row["action"] == "exit" and row["reason"] == reason for row in runs[case]["signals"]):
            raise AssertionError(f"Missing registered behavior {case}: {reason}")
    for case in ("zero_volatility", "unknown_qualification"):
        if any(row["action"] == "entry" for row in runs[case]["signals"]):
            raise AssertionError(f"Ineligible fixture opened: {case}")
    if not runs["hard_drift"]["accounting"]["risk_halted"]:
        raise AssertionError("Hard drift did not trigger frozen ledger risk gate")
    for case in ("shared_asset", "basket"):
        if not any(row["action"] == "entry" for row in runs[case]["signals"]):
            raise AssertionError(f"No entry in {case}")
    for run in runs.values():
        for row in run["accounting"]["ledger"]:
            if row["event"] in {"fill", "risk_fill"} and row["ts"] <= row["known_at"]:
                raise AssertionError("Same-time or anticipatory execution")


def render():
    runs = {case: run_case(case) for case in CASES}
    assert_behaviors(runs)
    out = {"synthetic.json": json.dumps(runs, ensure_ascii=False, indent=2, sort_keys=True) + "\n"}
    signals = [{"scenario": case, **event} for case, run in runs.items() for event in run["signals"]]
    out["signals.json"] = json.dumps(signals, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    stream = io.StringIO(newline="")
    fields = [
        "scenario",
        "ts",
        "event",
        "book",
        "symbol",
        "known_at",
        "equity",
        "cash",
        "gross_notional",
        "net_notional",
        "asset_absolute_notional",
        "single_leg_books",
        "positions",
        "detail",
    ]
    writer = csv.DictWriter(stream, fields, lineterminator="\n")
    writer.writeheader()
    for case, run in runs.items():
        for row in run["accounting"]["ledger"]:
            values = {k: row[k] for k in fields if k in row}
            values["detail"] = {k: v for k, v in row.items() if k not in fields}
            writer.writerow(
                {
                    "scenario": case,
                    **{
                        k: json.dumps(v, sort_keys=True) if isinstance(v, (list, dict)) else v
                        for k, v in values.items()
                    },
                }
            )
    out["ledger.csv"] = stream.getvalue()
    risk_stream = io.StringIO(newline="")
    risk_fields = [
        "scenario",
        "seq",
        "ts",
        "event",
        "equity",
        "gross_notional",
        "net_notional",
        "asset_absolute_notional",
        "gross_limit",
        "asset_limit",
        "gross_excess",
        "asset_excess",
        "hard_limit_breached",
        "risk_exit_fill",
        "reducing_existing_leg",
    ]
    risk_writer = csv.DictWriter(risk_stream, risk_fields, lineterminator="\n")
    risk_writer.writeheader()
    for case, run in runs.items():
        for row in run["risk_exposure"]["events"]:
            risk_writer.writerow(
                {
                    "scenario": case,
                    **{
                        k: json.dumps(v, sort_keys=True) if isinstance(v, dict) else v for k, v in row.items()
                    },
                }
            )
    out["risk_exposure.csv"] = risk_stream.getvalue()
    summary = {
        case: {
            "exit_reasons": sorted({r["reason"] for r in run["signals"] if r["action"] == "exit"}),
            "entries": sum(r["action"] == "entry" for r in run["signals"]),
            "holdings": run["holdings"],
            "risk_halted": run["accounting"]["risk_halted"],
            "breach_episodes": run["risk_exposure"]["episodes"],
            "reconciliation_error": run["accounting"]["reconciliation_error"],
        }
        for case, run in runs.items()
    }
    out["summary.json"] = (
        json.dumps(
            {
                "scope": "synthetic_only",
                "real_window_reads": 0,
                "qualification_tests_pending": [
                    "Engle-Granger cointegration",
                    "ADF/unit-root/stationarity",
                    "relationship stability",
                    "relative-return residual",
                    "point-in-time candidate qualification",
                    "prespecified family and multiple-testing-to-qualification wiring",
                    "FDR procedure",
                ],
                "holm_alpha": config()["holm_alpha"],
                "holm_scope": "external complete family FWER adjustment only; not FDR or qualification",
                "strategy_conclusion_permitted": False,
                "runs": summary,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return out


def verify_freeze():
    for name in ("M3_PREREGISTRATION.json", "M3_R2_PREREGISTRATION.json", FREEZE.name):
        spec = json.loads((ROOT / "research" / name).read_text(encoding="utf-8"))
        for path, expected in spec["sha256"].items():
            if sha((ROOT / path).read_bytes()) != expected:
                raise Rejected("freeze_mismatch", f"Frozen source changed: {path}")


def perform(action):
    verify_freeze()
    output = render()
    manifest = {
        name: {"bytes": len(body.encode()), "sha256": sha(body.encode())} for name, body in output.items()
    }
    expected = json.loads((ROOT / "research/M3_R2_EXPECTED_V2.json").read_text(encoding="utf-8"))
    if manifest != expected:
        raise Rejected("artifact_mismatch", "Synthetic results disagree with frozen expected hashes")
    output["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if action == "write":
        if RESULTS.exists():
            raise Rejected("frozen_directory_exists", "Never overwrite captured M3 results; use verify")
        RESULTS.mkdir(parents=True)
        for name, content in output.items():
            (RESULTS / name).write_text(content, encoding="utf-8")
    elif RESULTS.exists():
        if {p.name for p in RESULTS.iterdir()} != set(output):
            raise Rejected("artifact_mismatch", "Unexpected or missing M3 artifacts")
        for name, content in output.items():
            if sha((RESULTS / name).read_bytes()) != sha(content.encode()):
                raise Rejected("artifact_mismatch", f"M3 artifact mismatch: {name}")
    return {
        "scope": "SYNTHETIC_ONLY",
        "cases": len(CASES),
        "real_window_reads": 0,
        "manifest_sha256": sha(output["manifest.json"].encode()),
        "artifacts_present": RESULTS.exists(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "write", "real"))
    args = parser.parse_args()
    try:
        if args.action == "real":
            raise Rejected(
                "real_window_disabled", "M3 does not read real market windows or evaluate strategy"
            )
        guard_report = install()

        def run():
            result = perform(args.action)
            # Counts are added after the frozen result hashes are checked, so the
            # synthetic golden manifest stays byte-identical while this CLI run
            # still reports what the installed guard actually blocked.
            result["synthetic_guard"] = dict(guard_report)
            result["guard_scope"] = GUARD_SCOPE
            return result

        print(json.dumps(audited(args.action, run, freeze=FREEZE), sort_keys=True))
    except Rejected as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except PermissionError as exc:
        # A trip is evidence the guard ran; still report this process's counts.
        print(
            json.dumps(
                {
                    "synthetic_guard": dict(guard_report),
                    "guard_scope": GUARD_SCOPE,
                    "guard_error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()

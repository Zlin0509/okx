"""Offline metadata inspection, independent review and single approved evaluation."""

import argparse
from pathlib import Path

from ..engine import metrics
from ..protocol import COST_BPS, SYMBOLS
from ..run import cash_benchmarks, write_json
from ..stress import run_stress
from . import gate
from .evidence import digest, inspect_manifest
from .replay import simulate, synthetic_replay


def review(root, source_manifest, approval):
    if approval.review_sha256 is not None:
        raise ValueError("review requires isolation approval without evaluation authority")
    _, report = gate.validated_market(root, source_manifest, approval)
    return report


def _check_exposure(ledger):
    measured, suspected = [], []
    for row in ledger:
        for bucket, flag in ((measured, True), (suspected, False)):
            if any(
                event["valuation_measured"] is flag and event["cap_excess"]
                for event in row["execution_events"]
            ):
                bucket.append(row["day"])
    return measured, suspected


def _assess(defense, starting_usd, passive, stress_ok):
    result = metrics(defense, starting_usd, assess=True)
    measured, suspected = _check_exposure(defense)
    passive_complete = not passive[-1]["passive_incomplete"] and all(
        passive[-1]["quantity"][symbol] > 0 for symbol in SYMBOLS
    )
    result["event_exposure_breach_days"] = measured
    result["event_exposure_suspected_stale_days"] = suspected
    result["stale_execution_valuation"] = [
        {"day": row["day"], "assets": row["execution_valuation_stale_assets"]}
        for row in defense
        if row["execution_valuation_stale_assets"]
    ]
    result["stale_close_valuation"] = [
        {"day": row["day"], "assets": row["stale_eod"]} for row in defense if row["stale_eod"]
    ]
    if (
        result["stale_valuation_days"]
        or suspected
        or result["post_exposure_breaches_stale"]
        or not passive_complete
        or not stress_ok
    ):
        result["acceptance"] = "insufficient_evidence"
    else:
        result["acceptance"] = "pass" if all(result["numeric_checks"].values()) and not measured else "fail"
    return result


def evaluate(root, source_manifest, approval):
    if approval.review_sha256 is None:
        raise ValueError("release approval required")
    if gate.marker_path(approval).exists():
        raise FileExistsError("evaluation already claimed")
    market, report = gate.validated_market(root, source_manifest, approval)
    if digest(gate.report_bytes(report)) != approval.review_sha256 or not report["coverage"]["complete"]:
        raise ValueError("coverage is incomplete or differs from signed review")
    gate.claim(approval)
    output = approval.authority.state_directory / "zlin21-v2-results"
    output.mkdir()

    stress = run_stress(synthetic_replay)
    stress_ok = all(all(result["protections"].values()) for result in stress.values())
    summary = {
        "evidence": report,
        "stress_protections_passed": stress_ok,
        "by_cost_bp": {},
        "research_conclusion": "risk_thresholds_met",
    }
    for bp in COST_BPS:
        defense, starting_usd = simulate(market, bp)
        passive, passive_base = simulate(market, bp, passive=True)
        result = _assess(defense, starting_usd, passive, stress_ok)
        if result["acceptance"] == "insufficient_evidence":
            summary["research_conclusion"] = "retain_cash_due_to_insufficient_evidence"
        elif result["acceptance"] == "fail" and summary["research_conclusion"] == "risk_thresholds_met":
            summary["research_conclusion"] = "retain_cash"
        summary["by_cost_bp"][bp] = {
            "defense": result,
            "passive_btc_eth": metrics(passive, passive_base),
            **cash_benchmarks(defense, starting_usd),
        }
        write_json(output / f"{bp}bp-ledgers.json", {"defense": defense, "passive": passive})
    write_json(output / "stress.json", stress)
    write_json(output / "summary.json", summary)
    return summary


def entrypoint(arguments=None, *, authority=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("manifest", type=Path)
    inspect.add_argument("sha256")
    for mode in ("review", "evaluate"):
        command = commands.add_parser(mode)
        for name in ("evidence", "source_manifest", "isolation", "isolation_signature", "audit"):
            command.add_argument(name, type=Path)
        if mode == "evaluate":
            command.add_argument("release", type=Path)
            command.add_argument("release_signature", type=Path)
    args = parser.parse_args(arguments)
    if args.mode == "inspect":
        value = inspect_manifest(args.manifest.read_bytes(), expected_hash=args.sha256)
    else:
        # Never touch evidence or approval files when this candidate has no pinned authority.
        if authority is None:
            parser.error("independent reviewer authority has not been frozen")
        release = {}
        if args.mode == "evaluate":
            release = {
                "release": args.release.read_bytes(),
                "release_signature": args.release_signature.read_bytes(),
            }
        approval = gate.authorize(
            authority,
            now,
            args.isolation.read_bytes(),
            args.isolation_signature.read_bytes(),
            args.audit.read_bytes(),
            **release,
        )
        value = (
            review(args.evidence, args.source_manifest.read_bytes(), approval)
            if args.mode == "review"
            else evaluate(args.evidence, args.source_manifest.read_bytes(), approval)
        )
    print(gate.report_bytes(value).decode().rstrip())


if __name__ == "__main__":
    entrypoint()

"""Assemble an offline report from immutable R2/R3 experiment outputs."""

import json
from collections import Counter
from decimal import localcontext
from pathlib import Path

from ..engine import ONE, ZERO, Rejected, number
from ..execution_audit import sha
from ..experiments.acceptance import _metrics
from ..experiments.contract import protocol
from ..portfolio import DAY
from .attribution import EPS, analyze
from .metrics import align, available, correlations, daily_metrics, daily_path, require, unavailable, utc_date

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES = ("base", "stress")
RECOVERABLE = (KeyError, TypeError, ValueError, ArithmeticError, OSError)
PRIVATE_RESEARCH_DIRECTORIES = {
    "snapshots",
    "results",
    "audit",
    "ws_data",
    "ws_results",
    "ws_audit",
    "experiment_audit",
}


def _read(path, expected):
    require(path is not None, "reviewed_artifact_missing")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise Rejected("reviewed_artifact_missing", path.name) from exc
    require(
        not any(
            a == "research" and b in PRIVATE_RESEARCH_DIRECTORIES
            for a, b in zip(resolved.parts, resolved.parts[1:], strict=False)
        ),
        "controlled_artifact_path_refused",
    )
    binary = resolved.read_bytes()
    require(sha(binary) == expected, "reviewed_artifact_hash_mismatch")
    return json.loads(binary)


def _synthetic(data):
    require(
        data.get("origin") == "synthetic_test"
        and data.get("performance_evidence") is False
        and data.get("real_returns") is None,
        "non_synthetic_origin_refused",
    )


def _path_from_ledger(accounting):
    closes = {}
    for row in accounting["ledger"]:
        ts = row["ts"]
        if row["event"] == "frame_end" and type(ts) is int and ts % DAY == DAY - 1:
            date = ts - DAY + 1
            require(date not in closes, "duplicate_utc_daily_close")
            closes[date] = row["equity"]
    return daily_path(closes, accounting["initial_equity"], closes.values())


def _cost_and_turnover(accounting):
    rows = [r for r in accounting["ledger"] if r["event"] in {"fill", "risk_fill"}]
    total = sum((abs(number(x["executed_delta"])) * number(x["execution_price"]) for x in rows), ZERO)
    return {
        "absolute_fills_usdt": total,
        "over_initial_capital": total / number(accounting["initial_equity"]),
        "fill_count": len(rows),
    }


def _r2_case(row, cfg, scenario, frames, policy):
    _synthetic(row)
    require(row["config"] == cfg and row["scenario"] == scenario, "configuration_or_scenario_mismatch")
    require(row["costs"] == policy["cost_scenarios"][scenario], "cost_assumptions_mismatch")
    ledger = row["accounting"]
    path = _path_from_ledger(ledger)
    require(path["capital_usdt"] == number(policy["risk"]["initial_equity"]), "different_initial_capital")
    projection = tuple(ts for ts in frames if ts % DAY == DAY - 1)
    expected = tuple(day + DAY - 1 for day in path["days"])
    aligned_marks = expected == projection
    attribution = unavailable("missing_aligned_asset_calendar")
    concentration = unavailable("missing_aligned_asset_calendar")
    terminal = unavailable("event_reconciliation_unavailable")
    if aligned_marks:
        try:
            replay = analyze(ledger, frames)
            attribution = available(replay["attribution"])
            concentration = available(replay["concentration"])
            terminal = available(replay["terminal"])
        except RECOVERABLE as exc:
            reason = getattr(exc, "code", "event_reconciliation_failed")
            attribution = concentration = unavailable(reason)
    marks = (
        correlations(path, frames)
        if attribution["status"] == "available"
        else unavailable(attribution["reason"])
    )
    counts = Counter((event["action"], event["reason"]) for event in row["signals"])
    return {
        "status": "available",
        "origin": "synthetic_test",
        "costs": row["costs"],
        "signal_reasons": [
            {"action": action, "reason": reason, "count": count}
            for (action, reason), count in sorted(counts.items())
        ],
        "path": path,
        "first_utc_date": utc_date(path["days"][0]).isoformat(),
        "last_utc_date": utc_date(path["days"][-1]).isoformat(),
        "metrics": daily_metrics(path),
        "terminal_event": terminal,
        "turnover": available(_cost_and_turnover(ledger)),
        "concentration": concentration,
        "correlations": marks,
        "drawdown_attribution": attribution,
        "qualifying_round_trips": unavailable("R2_ledger_did_not_bind_frozen_entry_intents"),
    }


def _comparison(subject, reference):
    if subject["status"] != "available":
        return unavailable(subject["reason"])
    if reference["status"] != "available":
        return unavailable("comparison_reference_unavailable")
    reason = align(subject["path"], reference["path"])
    if reason:
        return unavailable(reason)
    a, b = (row["metrics"]["close_total_return"]["value"] for row in (subject, reference))
    return available(a - b)


def _r2(directory, anchors, frames, policy):
    runs = {}
    for config in policy["configs"]:
        cfg = config["id"]
        filename = cfg + ".json"
        try:
            file = _read(directory / filename if directory else None, anchors["R2"][filename])
        except RECOVERABLE as exc:
            reason = getattr(exc, "code", "invalid_reviewed_artifact")
            runs[cfg] = {s: unavailable(reason) for s in CASES}
            continue
        runs[cfg] = {}
        for scenario in CASES:
            try:
                runs[cfg][scenario] = _r2_case(file[scenario], cfg, scenario, frames, policy)
            except RECOVERABLE as exc:
                runs[cfg][scenario] = unavailable(getattr(exc, "code", "invalid_r2_ledger"))
    specs = {cfg["id"]: cfg for cfg in policy["configs"]}
    return {
        "runs": runs,
        "sensitivity": {
            cfg: {
                "configuration": specs[cfg],
                "base_minus_primary_close_return": _comparison(rows["base"], runs["primary"]["base"]),
                "stress_minus_primary_close_return": _comparison(rows["stress"], runs["primary"]["stress"]),
                "stress_minus_base_close_return": _comparison(rows["stress"], rows["base"]),
            }
            for cfg, rows in runs.items()
        },
    }


def _r3_path(series, scenario, nav=None):
    require(
        series["origin"] == "synthetic_test" and series["scenario"] == scenario,
        "non_synthetic_origin_refused",
    )
    days, raw = series["days"], tuple(map(number, series["values"]))
    require(len(days) == len(raw) and all(v > -ONE for v in raw), "invalid_daily_path")
    if nav is None:
        nav = []
        equity = number(series["capital_usdt"])
        for change in raw:
            equity *= ONE + change
            nav.append(equity)
    path = daily_path(days, series["capital_usdt"], nav)
    require(
        all(abs(a - b) <= number("1e-14") for a, b in zip(path["daily_returns"], raw, strict=True)),
        "r3_spot_returns_do_not_reconcile_nav",
    )
    return path


def _r3_series(series, scenario, *, nav=None, turnover=None):
    path = _r3_path(series, scenario, nav)
    risk = (
        unavailable("fewer_than_365_aligned_daily_observations")
        if len(path["days"]) < 365
        else available(_metrics(tuple(float(r) for r in path["daily_returns"])))
    )
    return {
        "status": "available",
        "origin": "synthetic_test",
        "path": path,
        "metrics": daily_metrics(path),
        "turnover": available(turnover) if turnover else unavailable("missing_turnover_proof"),
        "R3_annualized_risk": risk,
    }


def _spot(book, scenario):
    turnover = sum((number(f["notional"]) for f in book["fills"]), ZERO)
    capital = number(book["returns"]["capital_usdt"])
    require(capital > 0 and turnover >= 0, "invalid_spot_turnover")
    require(
        abs(turnover / capital - number(book["absolute_turnover_over_initial_capital"])) <= EPS,
        "r3_spot_turnover_does_not_reconcile",
    )
    return _r3_series(
        book["returns"],
        scenario,
        nav=book["nav"],
        turnover={
            "absolute_fills_usdt": turnover,
            "over_initial_capital": turnover / capital,
            "fill_count": len(book["fills"]),
        },
    )


def _r3_case(case, scenario, policy):
    proof = case["scenario_evidence"]
    require(
        proof["origin"] == "synthetic_test"
        and proof["scenario"] == scenario
        and proof["funding_calendar_and_marks_complete"] is True,
        "missing_r3_mock_cost_evidence",
    )
    ratio = number(proof["absolute_turnover_over_initial_capital"])
    require(ratio >= 0, "invalid_r3_strategy_turnover")
    strategy = _r3_series(
        case["strategy_daily_returns"],
        scenario,
        turnover={"over_initial_capital": ratio, "source": "frozen_scenario_evidence"},
    )
    require(
        strategy["path"]["capital_usdt"] == number(policy["risk"]["initial_equity"]),
        "different_initial_capital",
    )
    out = {"strategy": strategy}
    benchmarks = case.get("benchmarks", {})
    for key in ("btc", "equal_weight"):
        try:
            require(
                benchmarks.get("status") == "synthetic_diagnostic"
                and benchmarks.get("origin") == "synthetic_test"
                and benchmarks.get("registry_sha256"),
                "missing_r3_benchmark_qualification_evidence",
            )
            spot = _spot(benchmarks[key], scenario)
            mismatch = align(strategy["path"], spot["path"])
            out[key] = unavailable(mismatch) if mismatch else spot
        except RECOVERABLE as exc:
            out[key] = unavailable(getattr(exc, "code", "invalid_r3_benchmark"))
    return {
        "status": "available",
        **out,
        "close_return_differences": {key: _comparison(strategy, out[key]) for key in ("btc", "equal_weight")},
        "per_asset_correlations": unavailable("R3_no_aligned_single_asset_close_panel"),
        "drawdown_attribution": unavailable("R3_no_event_ledger_or_per_leg_marks"),
        "concentration": unavailable("R3_no_event_exposure_ledger"),
    }


def _r3(path, anchors, policy):
    try:
        body = _read(path, anchors["R3_acceptance_sha256"])
        _synthetic(body)
    except RECOVERABLE as exc:
        return {s: unavailable(getattr(exc, "code", "invalid_reviewed_artifact")) for s in CASES}
    results = {}
    for scenario in CASES:
        try:
            results[scenario] = _r3_case(body["scenarios"][scenario], scenario, policy)
        except RECOVERABLE as exc:
            results[scenario] = unavailable(getattr(exc, "code", "invalid_r3_ledger"))
    return results


def verify_reporting_source():
    freeze = (HERE / "REPORT_SOURCE_FREEZE_V1.json").read_bytes()
    for path, digest in json.loads(freeze)["sha256"].items():
        require(sha((ROOT / path).read_bytes()) == digest, "reporting_source_freeze_mismatch")
    return sha(freeze)


def report(r2, r3):
    from ..experiments.runner import mock_inputs, verify_source

    source_sha = verify_reporting_source()
    policy = protocol()
    anchors = json.loads((HERE / "INPUT_ANCHORS.json").read_bytes())
    require(sha(verify_source()) == anchors["source_freeze_sha256"], "reviewed_source_freeze_mismatch")
    market, _, _, _ = mock_inputs()
    with localcontext() as ctx:
        ctx.prec = 34
        frames = {frame.ts: frame.marks for frame in market.frames}
        wiring = _r2(r2, anchors, frames, policy)
        validation = _r3(r3, anchors, policy)
        preflight = {
            "listed_history": unavailable("missing_verified_historical_universe"),
            "test_eligibility": unavailable("missing_verified_eligible_test_dates"),
            "funding_costs": unavailable("missing_actual_historical_funding_and_settlement_marks"),
        }
        return {
            "schema": "ZLIN-11-synthetic-report-v1",
            "origin": "synthetic_test",
            "performance_evidence": False,
            "acceptance_passed": False,
            "real_metrics": unavailable("real_acceptance_disabled_pending_independent_preregistration"),
            "real_readiness": preflight,
            "inputs": anchors,
            "report_source_freeze_sha256": source_sha,
            "R2": wiring,
            "R3": validation,
            "R2_vs_R3": unavailable("different_fixtures_and_no_common_evaluation_calendar"),
        }

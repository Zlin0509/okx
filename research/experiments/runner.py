"""Executable synthetic experiments: PIT pool -> full screen -> decisions -> two costed ledgers."""

import argparse
import json
import subprocess
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from ..causal import CausalEngine
from ..engine import Costs, Frame, Market, Rejected, number, serializable, sign
from ..execution_audit import append, canonical, sha
from ..portfolio import DAY, holding_periods
from ..risk_trace import exposure_trace
from ..synthetic_guard import GUARD_SCOPE, install
from ..ws_view import readonly
from .contract import Journal, protocol, real_preflight
from .registry import build_registry
from .screen import screen
from .strategy import Strategy
from .universe import candidate_family, pool_at

ROOT = Path(__file__).resolve().parents[2]
FREEZE = Path(__file__).with_name("SOURCE_FREEZE_R3_V5.json")


class ExperimentEngine(CausalEngine):
    def __init__(self, market, turnover, costs):
        super().__init__(market, initial=protocol()["risk"]["initial_equity"], costs=costs)
        self.turnover = turnover
        self.daily_traded = defaultdict(lambda: number(0))

    def execute(self, frame, order):
        symbol = order.leg.symbol
        # Capacity denominator is frozen at signal decision, not updated with the intervening day's volume.
        close = (order.known_at // DAY - 1) * DAY + DAY - 1
        values = [self.turnover.get(symbol, {}).get(close - lag * DAY) for lag in range(30)]
        if any(
            v is None or v["available_at"] > order.known_at or str(v.get("confirm")) != "1" for v in values
        ):
            self.record(
                frame,
                "rejected_capacity",
                symbol=symbol,
                book=order.leg.book,
                reason="missing_lagged_turnover",
            )
            return
        limit = (
            sum((number(v["volCcyQuote"]) for v in values), number(0))
            / 30
            * number(protocol()["risk"]["daily_participation"])
        )
        if limit <= 0 or any(number(v["volCcyQuote"]) < 0 for v in values):
            self.record(
                frame,
                "rejected_capacity",
                symbol=symbol,
                book=order.leg.book,
                reason="invalid_or_nonpositive_turnover",
            )
            return
        old = self.positions.get(order.leg)
        quantity = old.quantity if old else number(0)
        delta = (order.target - quantity) * order.fraction
        price = frame.execution[symbol] * (1 + sign(delta) * number(self.costs.slippage_bps) / 10000)
        notional = abs(delta) * price
        key = (frame.ts // DAY, symbol)
        if self.daily_traded[key] + notional > limit:
            self.record(
                frame,
                "rejected_capacity",
                symbol=symbol,
                book=order.leg.book,
                reason="absolute_daily_participation",
                requested_notional=notional,
                already_traded=self.daily_traded[key],
                limit=limit,
            )
            return
        before = len(self.ledger)
        super().execute(frame, order)
        if len(self.ledger) > before and self.ledger[-1]["event"] in {"fill", "risk_fill"}:
            self.daily_traded[key] += notional


def mock_inputs(series=None):
    from ..statistics.fixtures import synthetic_series

    p = protocol()["synthetic"]
    series = series or synthetic_series()
    symbols = tuple(p["symbols"])
    observations = {t: row for t, row in series.items()}
    metadata = {
        s: [
            {
                "known_at": -1000 * DAY,
                "effective_at": -1000 * DAY,
                "listing": -1000 * DAY,
                "continuous": -1000 * DAY,
                "verified": True,
                "tradable": True,
                "delisting": None,
            }
        ]
        for s in symbols
    }
    turnover = {
        s: {t: {"volCcyQuote": str(20000000 + i * 1000), "available_at": t, "confirm": "1"} for t in series}
        for i, s in enumerate(symbols)
    }
    start, stop = p["screen_day"] + 1, p["end_day_exclusive"]
    frames = []
    for day in range(start, stop):
        prior, current = observations[day * DAY - 1], observations[(day + 1) * DAY - 1]
        opening = {s: prior.prices[s] for s in symbols}
        frames += [
            Frame(day * DAY, opening, opening),
            Frame((day + 1) * DAY - 1, {s: current.prices[s] for s in symbols}, {}),
        ]
    # Explicit synthetic next opening for boundary exits, not a fabricated terminal fill.
    opening = {s: observations[stop * DAY - 1].prices[s] for s in symbols}
    frames.append(Frame(stop * DAY, opening, opening))
    opens = tuple(range(start * DAY, (stop + 1) * DAY, DAY))
    market = Market(
        tuple(frames),
        tuple(f.ts for f in frames),
        dict.fromkeys(symbols, opens),
        dict.fromkeys(symbols, opens),
        {
            (t, s): number("0.0001") * (1 if (t // DAY + i) % 2 else -1)
            for t in opens
            for i, s in enumerate(symbols)
        },
        "synthetic_test",
        "SYNTHETIC daily settlements, no claim of exchange cadence",
    )
    return market, observations, metadata, turnover


def validate_adapter(market, *, funding_kind, origin):
    if origin != "synthetic_test" or market.provenance != origin:
        raise Rejected("real_execution_disabled", "No real costed experiment before independent gates")
    if funding_kind != "synthetic_settlement":
        raise Rejected("funding_snapshot_not_settlement", "State snapshots never create settlement cashflows")
    market.validate()  # Reject missing rates, exact calendar or settlement marks; never zero-fill.


def run_configuration(cfg, batch, inputs, scenario):
    from ..statistics.core import Candidate, FamilyPlan, window_data
    from ..statistics.fixtures import asof
    from .screen import check_batch

    market, observations, metadata, turnover = inputs
    batches = {int(t): value for t, value in batch.items()} if "decision_ts" not in batch else {}
    if batches:
        batch = batches[min(batches)]
    p = protocol()
    if cfg not in p["configs"]:
        raise Rejected("unregistered_experiment", "Exact six configuration definitions required")
    for checked in batches.values() if batches else [batch]:
        check_batch(checked)
        if checked["config_ids"] != [c["id"] for c in p["configs"]]:
            raise Rejected("unregistered_experiment", "Complete six-configuration family required")
        screen_view = asof(observations, checked["decision_ts"] // DAY)
        decision_ts = checked.get("registry_decision_ts", screen_view.ts)
        pool = pool_at(decision_ts, metadata, turnover, screen_view.observations, universe_verified=True)
        if pool["status"] != "eligible" or canonical(
            candidate_family(pool["members"], p["synthetic"]["core_anchors"])
        ) != canonical(checked["family"]):
            raise Rejected(
                "pool_family_mismatch", "Statistical family must match the as-of admitted universe"
            )
        plan = FamilyPlan(
            "experiment_R1_frozen_pool",
            tuple(Candidate(c["id"], tuple(c["symbols"])) for c in checked["family"]),
        )
        for candidate in plan.candidates:
            _, sample = window_data(screen_view, candidate, plan)
            if (
                candidate.id in checked["analyses"]
                and sample["training_sha256"] != checked["analyses"][candidate.id]["training_sha256"]
            ):
                raise Rejected(
                    "training_mismatch", "Every candidate binds the actual historical observations"
                )
    validate_adapter(market, funding_kind="synthetic_settlement", origin="synthetic_test")
    end_ts = (protocol()["synthetic"]["end_day_exclusive"] - 1) * DAY
    strategy = None if batches else Strategy(cfg, batch, end_ts)
    pools = []

    def driver(frame):
        nonlocal strategy
        if frame.ts in batches:
            from .screen import check_batch

            current_batch = batches[frame.ts]
            check_batch(current_batch)
            if current_batch["decision_ts"] > frame.ts:
                raise Rejected("future_statistics", "No future screening passed to strategy")
            if strategy is None:
                strategy = Strategy(cfg, current_batch, end_ts)
            else:
                strategy.batch = current_batch
        pool = (
            pool_at(frame.ts, metadata, turnover, frame.observations, universe_verified=True)
            if frame.ts % DAY == 0
            else {"status": "not_a_decision", "members": []}
        )
        if frame.ts % DAY == 0:
            pools.append({"ts": frame.ts, **pool})
        if strategy is None:
            from ..decision_view import Decision

            return Decision()
        return strategy(frame, readonly(pool))

    cost = protocol()["cost_scenarios"][scenario]
    result = ExperimentEngine(
        market, turnover, Costs(number(cost["fee_bps"]), number(cost["slippage_bps"]))
    ).run_causal(driver, observations)
    if abs(result["reconciliation_error"]) > number("1e-24"):
        raise AssertionError("Synthetic accounting reconciliation failed")
    costs = [
        {
            k: row.get(k)
            for k in (
                "ts",
                "known_at",
                "event",
                "book",
                "symbol",
                "executed_delta",
                "reference_price",
                "execution_price",
                "fee",
                "slippage_diagnostic",
                "rate",
                "settlement_mark",
                "cash_delta",
            )
        }
        for row in result["ledger"]
        if row["event"] in {"fill", "risk_fill", "funding"}
    ]
    return serializable(
        {
            "origin": "synthetic_test",
            "performance_evidence": False,
            "config": cfg["id"],
            "scenario": scenario,
            "costs": cost,
            "pools": pools,
            "signals": strategy.events if strategy is not None else [],
            "cost_ledger": costs,
            "accounting": result,
            "holding_periods": holding_periods(result["ledger"]),
            "qualifying_relationship_round_trips": None,
            "baseline_metrics": None,
            "bootstrap_intervals": None,
            "exposure": exposure_trace(result["ledger"]),
            "real_returns": None,
            "annualized_metrics": None,
            "acceptance_passed": False,
        }
    )


def verify_source():
    source = FREEZE.read_bytes()
    for path, value in json.loads(source)["sha256"].items():
        if sha((ROOT / path).read_bytes()) != value:
            raise Rejected("freeze_mismatch", path)
    return source


def synthetic(output):
    from ..statistics.fixtures import asof

    p = protocol()
    source = verify_source()
    output = Path(output)
    if output.exists():
        raise Rejected("frozen_directory_exists", "Keep all experiments and failed evidence")
    output.mkdir(parents=True)
    journal = Journal(output / "experiments.jsonl")
    inputs = mock_inputs()
    market, series, metadata, turnover = inputs
    registry = build_registry(market, series, metadata, turnover, universe_verified=True)
    (output / "registry.json").write_bytes(canonical(registry))
    if not registry or any(e["status"] != "available" for e in registry):
        raise Rejected(
            "point_in_time_registry_unavailable", "Complete metadata-only registry must precede all tests"
        )
    family = registry[0]["family"]
    binding = {
        "data_sha256": sha(
            canonical(
                serializable(
                    {
                        "market": asdict(market),
                        "observations": [
                            [t, row.available_at, dict(row.prices)] for t, row in sorted(series.items())
                        ],
                        "metadata": metadata,
                        "turnover": turnover,
                    }
                )
            )
        ),
        "family_sha256": sha(canonical(registry)),
        "source_sha256": sha(source),
        "dates_sha256": sha(canonical(market.frame_calendar)),
    }
    # Reserve the complete round before computing any new statistical result, even when it fails.
    tokens = {
        c["id"]: journal.reserve(c["id"], "training_validation", binding, origin="synthetic_test")
        for c in p["configs"]
    }
    try:
        batches = {}
        for entry in registry:
            view = asof(series, entry["decision_ts"] // DAY - 1)
            screened = screen(view, entry["family"], origin="synthetic_test")
            screened["registry_decision_ts"] = entry["decision_ts"]
            screened["registry_sha256"] = entry["registry_sha256"]
            screened["family_sha256"] = sha(
                canonical({k: v for k, v in screened.items() if k != "family_sha256"})
            )
            batches[entry["decision_ts"]] = screened
        (output / "screen.json").write_bytes(canonical(batches))
        batch = batches[min(batches)]
        runs = {}
        for cfg in p["configs"]:
            runs[cfg["id"]] = {
                cost: run_configuration(cfg, batches, inputs, cost) for cost in p["cost_scenarios"]
            }
            body = canonical(runs[cfg["id"]])
            (output / (cfg["id"] + ".json")).write_bytes(body)
            journal.finish(
                tokens[cfg["id"]],
                {"output_sha256": sha(body), "scope": "synthetic_accounting_not_return_evidence"},
                "passed",
            )
    except Exception as exc:
        for token in tokens.values():
            try:
                journal.finish(
                    token,
                    {"code": getattr(exc, "code", "pipeline_failure"), "error_type": type(exc).__name__},
                    "failed",
                )
            except Rejected:
                pass  # A completed slot remains completed; only unfinished slots are failed.
        raise
    manifest = {path.name: sha(path.read_bytes()) for path in output.iterdir() if path.suffix == ".json"}
    summary = {
        "status": "synthetic_wiring_completed",
        "scheduled_decisions": len(registry),
        "family_candidates": len(family),
        "primary_holm_members_per_decision": registry[0]["expected_counts"]["primary_holm"],
        "BY_EG_members_per_decision": registry[0]["expected_counts"]["BY_EG"],
        "configuration_members": len(family) * 6,
        "test_ids": len(batch["tests"]),
        "unavailable_test_ids": len(batch["unavailable_test_ids"]),
        "screen_status": batch["status"],
        "configuration_runs": 6,
        "cost_scenarios_each": 2,
        "real_preflight": real_preflight(),
        "real_returns": None,
        "acceptance_passed": False,
        "manifest": manifest,
    }
    (output / "summary.json").write_bytes(canonical(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("synthetic", "acceptance-synthetic", "preflight", "real"))
    parser.add_argument("--output", default="research/runtime/experiments_r2")
    args = parser.parse_args()
    counts = install()
    from .input_guard import install as install_input_guard

    private_counts = install_input_guard(ROOT)
    journal = ROOT / "research/runtime/experiment_attempts.jsonl"
    run_id = str(uuid.uuid4())
    common = {
        "run_id": run_id,
        "operation": "experiments_R2",
        "action": args.action,
        "HEAD": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": sha(FREEZE.read_bytes()),
        "python": sys.version.split()[0],
    }
    from datetime import UTC, datetime

    append(journal, {**common, "event": "start", "at_utc": datetime.now(UTC).isoformat()})
    try:
        if args.action == "synthetic":
            result = synthetic(args.output)
        elif args.action == "acceptance-synthetic":
            from .acceptance_fixture import write_exercise

            verify_source()
            result = write_exercise(args.output)
        else:
            result = real_preflight()
        result["guard_counts"] = dict(counts)
        result["private_input_guard"] = dict(private_counts)
        result["guard_scope"] = GUARD_SCOPE
        if any(counts.values()) or any(private_counts.values()):
            raise PermissionError("unexpected_cache_or_network_access")
        status = "refused" if result["status"] == "refused" else "passed"
    except Exception as exc:
        result = {
            "status": "failed",
            "reason": getattr(exc, "code", type(exc).__name__),
            "guard_counts": dict(counts),
            "private_input_guard": dict(private_counts),
        }
        status = "failed"
    append(
        journal,
        {
            **common,
            "event": "finish",
            "at_utc": datetime.now(UTC).isoformat(),
            "status": status,
            "result": result,
            "result_sha256": sha(canonical(result)),
        },
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(2 if status == "failed" or args.action == "real" else 0)

"""Independent synthetic exercise of the frozen R1 gates and R2 uncertainty definition."""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import stdev

from ..engine import Rejected
from .contract import protocol, select_validation
from .evaluation_inputs import ORIGIN, DailyReturns, daily_grid, unavailable

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ScenarioEvidence:
    """Keep participation and risk failures distinct from missing ledger evidence."""

    origin: str
    scenario: str
    funding_calendar_and_marks_complete: bool | None
    risk_limits_met: bool | None
    participation_limit_met: bool | None
    absolute_turnover_over_initial_capital: float | None


def _check_series(series, dates, scenario):
    if not isinstance(series, DailyReturns) or series.origin != ORIGIN:
        raise Rejected("synthetic_daily_series_required", "Real series are disabled")
    if (
        series.days != dates
        or series.scenario != scenario
        or len(series.values) != len(dates)
        or series.capital_usdt != protocol()["risk"]["initial_equity"]
    ):
        raise Rejected(
            "unaligned_calendar_capital_or_costs", "Require identical UTC dates, capital and scenario"
        )
    if any(type(v) not in (float, int) or not math.isfinite(v) or v <= -1 for v in series.values):
        raise Rejected("invalid_daily_return", "Finite daily equity returns above -100% required")


def _aligned(days, strategy, btc, equal_weight):
    dates = daily_grid(days)
    scenario = strategy.scenario if isinstance(strategy, DailyReturns) else None
    if scenario not in protocol()["cost_scenarios"]:
        raise Rejected("missing_or_unregistered_cost_scenario", "Use the frozen base or stress costs")
    paths = {"strategy": strategy, "btc": btc, "equal_weight": equal_weight}
    for series in paths.values():
        _check_series(series, dates, scenario)
    return paths


def _metrics(values):
    log_wealth, log_peak, drawdown = 0.0, 0.0, 0.0
    for v in values:
        log_wealth += math.log1p(v)
        log_peak = max(log_peak, log_wealth)
        drawdown = max(drawdown, -math.expm1(log_wealth - log_peak))
    cagr = math.expm1(log_wealth * 365 / len(values))
    deviation = stdev(values) if len(values) > 1 else 0
    return {
        "total_return": math.expm1(log_wealth),
        "cagr": cagr,
        "sharpe": (math.fsum(values) / len(values)) * math.sqrt(365) / deviation if deviation else None,
        "max_drawdown": drawdown,
        "calmar": cagr / drawdown if drawdown > 0 else None,
    }


def paired_bootstrap(*, origin, eligible_days, strategy, btc, equal_weight):
    """Use one common circular sequence of length-N indices for all three return paths."""
    if origin != ORIGIN:
        return unavailable("real_bootstrap_disabled")
    try:
        paths = _aligned(eligible_days, strategy, btc, equal_weight)
    except (TypeError, Rejected) as exc:
        return unavailable(getattr(exc, "code", "missing_eligible_dates"))
    spec = json.loads((ROOT / "DECISIONS_R2.json").read_text())["bootstrap"]
    n = len(strategy.days)
    block, replicates, seed = (spec[k] for k in ("block_days", "replicates", "seed"))
    if n < block:
        return unavailable("fewer_than_28_aligned_daily_observations")

    import numpy as np

    rng = np.random.Generator(np.random.PCG64(seed))
    samples = {}
    # Start each block uniformly, wrap around the original calendar and truncate the
    # last sampled block so every replicate has exactly the original number of days.
    source = np.asarray([s.values for s in paths.values()], dtype=np.float64)
    offsets = np.arange(block)
    for first in range(0, replicates, 128):
        batch = min(128, replicates - first)
        starts = rng.integers(n, size=(batch, (n + block - 1) // block))
        take = ((starts[:, :, None] + offsets) % n).reshape(batch, -1)[:, :n]
        lifted = source[:, take]
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            log_path = np.cumsum(np.log1p(lifted), axis=-1)
            peak = np.maximum.accumulate(np.maximum(log_path, 0), axis=-1)
            maxdd = np.max(-np.expm1(log_path - peak), axis=-1)
            log_final = log_path[:, :, -1]
            cagr = np.expm1(log_final * (365 / n))
            sd = lifted.std(axis=-1, ddof=1)
            distinct = lifted.max(axis=-1) != lifted.min(axis=-1)
            sharpe_ratio = np.where(distinct & (sd > 0), lifted.mean(axis=-1) * math.sqrt(365) / sd, np.nan)
            active = {
                "strategy_minus_cash": np.expm1(log_final[0]),
                "strategy_minus_btc": np.exp(log_final[0]) - np.exp(log_final[1]),
                "strategy_minus_equal_weight": np.exp(log_final[0]) - np.exp(log_final[2]),
            }
        for i, name in enumerate(paths):
            for metric, values in (("cagr", cagr), ("sharpe", sharpe_ratio), ("max_drawdown", maxdd)):
                samples.setdefault(f"{name}.{metric}", []).extend(values[i])
        for name, values in active.items():
            samples.setdefault(name, []).extend(values)

    intervals = {}
    for name, sample in samples.items():
        finite = np.asarray(sample)[np.isfinite(sample)]
        intervals[name] = {
            "percentile_2_5_97_5": np.percentile(finite, [2.5, 97.5], method="linear").tolist()
            if finite.size
            else None,
            "defined": int(finite.size),
            "undefined": replicates - int(finite.size),
        }
    return {
        "status": "synthetic_diagnostic",
        "origin": ORIGIN,
        "performance_evidence": False,
        "scenario": strategy.scenario,
        "block_days": block,
        "replicates": replicates,
        "seed": seed,
        "intervals": intervals,
    }


def _evidence(proof, scenario):
    if (
        not isinstance(proof, ScenarioEvidence)
        or proof.origin != ORIGIN
        or proof.scenario != scenario
        or proof.funding_calendar_and_marks_complete is not True
        or type(proof.risk_limits_met) is not bool
        or type(proof.participation_limit_met) is not bool
        or type(proof.absolute_turnover_over_initial_capital) not in (int, float)
        or not math.isfinite(proof.absolute_turnover_over_initial_capital)
        or proof.absolute_turnover_over_initial_capital < 0
    ):
        raise Rejected("missing_cost_risk_or_participation_evidence", scenario)


def _trip_count(trips, days):
    if (
        not trips
        or trips.get("status") != "synthetic_diagnostic"
        or trips.get("origin") != ORIGIN
        or trips.get("eligible_days") != days
        or trips.get("scenario") != "base"
        or type(trips.get("qualifying_count")) is not int
        or trips["qualifying_count"] < 0
    ):
        raise Rejected("missing_qualified_round_trip_report", "Require count on the same eligible split")
    if trips["qualifying_count"] != sum(
        episode.get("status") == "qualifying_round_trip" for episode in trips.get("episodes", ())
    ):
        raise Rejected("inconsistent_qualifying_round_trip_count", "Episode rows and count must agree")
    return trips["qualifying_count"]


def joint_acceptance(*, origin, eligible_days, base, stress, trips, validation, selected_config, proofs):
    """Numerical pass/fail for fabricated fixtures. Actual acceptance stays unavailable."""
    if origin != ORIGIN:
        return unavailable("real_acceptance_disabled_pending_independent_preregistration")
    try:
        days = daily_grid(eligible_days)
        if set(base) != {"strategy", "btc", "equal_weight"} or set(stress) != set(base):
            raise Rejected("missing_costed_comparators", "Base and stress require strategy, BTC and PIT spot")
        paths = {"base": base, "stress": stress}
        for scenario, series in paths.items():
            for s in series.values():
                _check_series(s, days, scenario)
            _evidence(proofs.get(scenario), scenario)
        count = _trip_count(trips, days)
    except (AttributeError, TypeError, Rejected) as exc:
        return unavailable(getattr(exc, "code", "missing_acceptance_input"))

    selection_issue = None
    try:
        selected = select_validation(validation)
    except Rejected as exc:
        if exc.code not in {"neighbour_robustness_failed", "no_qualified_validation_configuration"}:
            return unavailable(exc.code)
        selected, selection_issue = None, exc.code
    except (AttributeError, TypeError):
        return unavailable("missing_frozen_validation_selection")

    limits = json.loads((ROOT / "ACCEPTANCE_R1.json").read_text())["confirmatory_gates"]
    if len(days) < limits["minimum_eligible_test_days"]:
        return unavailable("fewer_than_365_eligible_test_days")
    try:
        metrics = {name: {k: _metrics(s.values) for k, s in series.items()} for name, series in paths.items()}
    except (OverflowError, ValueError):
        return unavailable("undefined_daily_path_metrics")
    for scenario in metrics.values():
        for result in scenario.values():
            if any(v is not None and not math.isfinite(v) for v in result.values()):
                return unavailable("undefined_daily_path_metrics")
    base_strategy, stress_strategy = metrics["base"]["strategy"], metrics["stress"]["strategy"]
    sharpe = base_strategy["sharpe"]
    comparators = (metrics["base"]["btc"]["sharpe"], metrics["base"]["equal_weight"]["sharpe"])
    gates = {
        "validation_selection": selected is not None and selected == selected_config,
        "eligible_days": True,
        "completed_relation_trades": count >= limits["minimum_completed_relationship_round_trips"],
        "base_net_cagr": base_strategy["cagr"] >= limits["base_net_cagr_min"],
        "base_daily_sharpe": sharpe is not None and sharpe >= limits["base_annualized_daily_sharpe_min"],
        "base_max_drawdown": base_strategy["max_drawdown"] <= limits["base_max_drawdown_max"],
        "base_calmar": base_strategy["calmar"] is not None
        and base_strategy["calmar"] >= limits["base_calmar_min"],
        "stress_net_cagr": stress_strategy["cagr"] >= limits["stress_net_cagr_min"],
        "stress_daily_sharpe": stress_strategy["sharpe"] is not None
        and stress_strategy["sharpe"] >= limits["stress_annualized_daily_sharpe_min"],
        "stress_max_drawdown": stress_strategy["max_drawdown"] <= limits["stress_max_drawdown_max"],
        "positive_active_against_cash": base_strategy["total_return"] > 0,
        "sharpe_at_least_both_spot_benchmarks": sharpe is not None
        and all(s is not None and sharpe >= s for s in comparators),
        "risk_limits": all(proofs[x].risk_limits_met for x in paths),
        "participation_limit": all(proofs[x].participation_limit_met for x in paths),
    }
    return {
        "status": "synthetic_diagnostic",
        "origin": ORIGIN,
        "performance_evidence": False,
        "real_acceptance_status": "unavailable",
        "acceptance_passed": False,
        "synthetic_numerical_gates_passed": all(gates.values()),
        "gates": gates,
        "validation_selection_issue": selection_issue,
        "eligible_day_count": len(days),
        "qualifying_count": count,
        "metrics": metrics,
        "turnover": {x: proofs[x].absolute_turnover_over_initial_capital for x in paths},
        "active_total_return_differences": {
            x: {
                ref: row["strategy"]["total_return"] - (row[ref]["total_return"] if ref != "cash" else 0)
                for ref in ("cash", "btc", "equal_weight")
            }
            for x, row in metrics.items()
        },
        "executable_capacity": None,
        "real_returns": None,
    }

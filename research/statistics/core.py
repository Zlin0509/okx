"""Pinned-library statistics on an immutable as-of snapshot, with complete-family gates."""

import hashlib
import json
import math
import re
import warnings
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path

import numpy as np
from statsmodels.regression.linear_model import OLS
from statsmodels.stats.diagnostic import breaks_cusumolsresid
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.stattools import adfuller, coint

from ..decision_view import DecisionSnapshot
from ..engine import Rejected
from ..spreads import Model, logs

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400000


def config():
    return json.loads((ROOT / "M3B_PROTOCOL.json").read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def dependencies():
    lock = (Path(__file__).parent / "requirements.lock").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^([a-zA-Z0-9_-]+)==([^\s\\]+)", lock, re.MULTILINE))
    actual = {name: version(name) for name in pins}
    if actual != pins:
        raise Rejected("dependency_mismatch", "Use the complete pinned M3B requirements.lock")
    return actual


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FamilyPlan:
    id: str
    candidates: tuple[Candidate, ...]
    planned_at: int = -1
    window: int = 512

    def validate(self):
        if (
            not self.id
            or not self.candidates
            or len({c.id for c in self.candidates}) != len(self.candidates)
            or len({c.symbols for c in self.candidates}) != len(self.candidates)
            or type(self.planned_at) is not int
            or type(self.window) is not int
            or self.window < config()["min_observations"]
        ):
            raise Rejected(
                "invalid_family_plan", "Unique prespecified candidates and sufficient window required"
            )
        for c in self.candidates:
            if (
                not c.id
                or ":" in c.id
                or len(c.symbols) < 2
                or len(set(c.symbols)) != len(c.symbols)
                or any(not isinstance(s, str) or not s or ":" in s for s in c.symbols)
            ):
                raise Rejected("invalid_family_plan", "Candidate direction and distinct symbols required")


@dataclass(frozen=True, slots=True)
class Eligibility:
    status: str = "unknown"
    known_at: int = 0
    source: str = "unknown"


def test_ids(candidate):
    return (candidate.id + ":eg", candidate.id + ":cusum", candidate.id + ":wald") + tuple(
        candidate.id + ":" + kind + ":" + s for kind in ("level", "diff") for s in candidate.symbols
    )


def window_data(view, candidate, plan):
    if not isinstance(view, DecisionSnapshot):
        raise Rejected("snapshot_required", "Use the reviewed immutable decision interface")
    times = sorted(t for t, row in view.observations.items() if t < view.ts and row.available_at <= view.ts)
    times = times[-plan.window :]
    if len(times) != plan.window:
        raise Rejected("insufficient_sample", "Do not silently shorten the prespecified training window")
    if times[-1] != view.ts - DAY or any(b - a != DAY for a, b in zip(times, times[1:], strict=False)):
        raise Rejected("missing_observation", "Consecutive lagged UTC observations required")
    if plan.planned_at > times[0]:
        raise Rejected("late_family_plan", "Register the family before its training observations")
    rows, values = [], []
    for t in times:
        row = view.observations[t]
        if row.ts != t or row.available_at < t:
            raise Rejected("invalid_availability", "Observation time and availability disagree")
        values.append(logs(row.prices, candidate.symbols))
        rows.append([t, row.available_at, [str(row.prices[s]) for s in candidate.symbols]])
    return np.asarray(values, dtype=float), {
        "train_start": times[0],
        "train_end": times[-1],
        "nobs": len(times),
        "training_sha256": digest(rows),
    }


def scalar(value):
    result = float(value)
    if not math.isfinite(result):
        raise Rejected("invalid_statistic", "Nonfinite statistics cannot grant a permit")
    return result


def stat_record(stat, pvalue, null, **detail):
    pvalue = scalar(pvalue)
    if not 0 <= pvalue <= 1:
        raise Rejected("invalid_pvalue", "Expected actual finite p-value in [0,1]")
    return {"statistic": scalar(stat), "pvalue": pvalue, "null": null, **detail}


def analyze(view, plan, candidate):
    data, sample = window_data(view, candidate, plan)
    p = config()
    tests = {}
    x = np.column_stack((np.ones(len(data)), data[:, 1:]))
    if np.linalg.matrix_rank(x) != x.shape[1]:
        raise Rejected("singular_training", "Full-rank price regressors required")
    # Reject numerical/statistical warnings, rather than turning them into fabricated passing tests.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        price = OLS(data[:, 0], x).fit()
        sd = scalar(np.std(price.resid, ddof=1))
        if sd < 1e-10:
            raise Rejected("zero_residual_scale", "Degenerate cointegration is not an executable relation")
        eg = coint(
            data[:, 0],
            data[:, 1:],
            trend=p["price_trend"],
            method="aeg",
            maxlag=p["adf_lags"],
            autolag=None,
            return_results=False,
        )
        tests[candidate.id + ":eg"] = stat_record(
            eg[0], eg[1], "no_cointegration", critical_values=[scalar(v) for v in eg[2]]
        )
        # Only its statistic is meaningful for checking EG's residual step; ordinary ADF p is discarded.
        resid_adf_stat = scalar(adfuller(price.resid, regression="n", maxlag=p["adf_lags"], autolag=None)[0])
        for i, symbol in enumerate(candidate.symbols):
            for kind, series in (("level", data[:, i]), ("diff", np.diff(data[:, i]))):
                result = adfuller(series, regression="c", maxlag=p["adf_lags"], autolag=None)
                tests[candidate.id + ":" + kind + ":" + symbol] = stat_record(
                    result[0],
                    result[1],
                    "unit_root",
                    used_lags=int(result[2]),
                    effective_nobs=int(result[3]),
                    critical_values={k: scalar(v) for k, v in result[4].items()},
                )
        returns = np.diff(data, axis=0)
        xr = np.column_stack((np.ones(len(returns)), returns[:, 1:]))
        if np.linalg.matrix_rank(xr) != xr.shape[1]:
            raise Rejected("singular_training", "Full-rank return regressors required")
        ret = OLS(returns[:, 0], xr).fit()
        if scalar(np.std(ret.resid, ddof=1)) < 1e-10:
            raise Rejected("zero_residual_scale", "Return residual requires nonzero scale")
        cusum = breaks_cusumolsresid(ret.resid, ddof=xr.shape[1])
        tests[candidate.id + ":cusum"] = stat_record(cusum[0], cusum[1], "return_parameters_stable")
        split = np.arange(len(returns)) >= int(len(returns) * p["fixed_break_fraction"])
        xb = np.column_stack((xr, xr * split[:, None]))
        if np.linalg.matrix_rank(xb) != xb.shape[1]:
            raise Rejected("singular_training", "Full-rank fixed-break design required")
        broken = OLS(returns[:, 0], xb).fit(
            cov_type="HAC",
            cov_kwds={
                "maxlags": p["HAC_maxlags"],
                "kernel": p["HAC_kernel"],
                "use_correction": p["HAC_small_sample_correction"],
            },
            use_t=False,
        )
        k = xr.shape[1]
        wald = broken.wald_test(np.column_stack((np.zeros((k, k)), np.eye(k))), use_f=False, scalar=True)
        tests[candidate.id + ":wald"] = stat_record(
            wald.statistic, wald.pvalue, "fixed_midpoint_coefficients_equal"
        )
        ar = OLS(price.resid[1:], np.column_stack((np.ones(len(data) - 1), price.resid[:-1]))).fit()
        phi = scalar(ar.params[1])
        half = -math.log(2) / math.log(phi) if 0 < phi < 1 else None
    return {
        "id": candidate.id,
        "symbols": list(candidate.symbols),
        "decision_ts": view.ts,
        **sample,
        "tests": tests,
        "residual_adf_statistic_only": resid_adf_stat,
        "residual_adf_ordinary_pvalue_used": False,
        "phi": phi,
        "half_life_days": half,
        "price_model": {
            "intercept": scalar(price.params[0]),
            "beta": [scalar(v) for v in price.params[1:]],
            "mean": scalar(np.mean(price.resid)),
            "sd": sd,
        },
        "return_model": {
            "intercept": scalar(ret.params[0]),
            "beta": [scalar(v) for v in ret.params[1:]],
            "mean": scalar(np.mean(ret.resid)),
            "sd": scalar(np.std(ret.resid, ddof=1)),
        },
    }


def adjust(tests, ids, method="holm"):
    reject, corrected, *_ = multipletests(
        [tests[i]["pvalue"] for i in ids], alpha=config()["alpha"], method=method
    )
    return {
        name: {"adjusted_p": scalar(value), "reject_null": bool(rej)}
        for name, value, rej in zip(ids, corrected, reject, strict=True)
    }


def decide_family(view, plan, analyses, eligibility, origin=None):
    if origin != "synthetic_test":
        raise Rejected("real_statistics_disabled", "Explicit synthetic provenance required")
    plan.validate()
    if set(analyses) != {c.id for c in plan.candidates}:
        raise Rejected("incomplete_test_family", "Do not drop/add candidates after observing results")
    tests = {}
    for c in plan.candidates:
        row = analyses[c.id]
        if (
            set(row["tests"]) != set(test_ids(c))
            or row["decision_ts"] != view.ts
            or tuple(row["symbols"]) != c.symbols
        ):
            raise Rejected("incomplete_test_family", "All prespecified tests and bindings must be present")
        _, sample = window_data(view, c, plan)
        if row["training_sha256"] != sample["training_sha256"]:
            raise Rejected("training_mismatch", "Every family member must match this historical snapshot")
        for record in row["tests"].values():
            if not 0 <= scalar(record["pvalue"]) <= 1:
                raise Rejected("invalid_pvalue", "Invalid family p-value")
            scalar(record["statistic"])
        tests.update(row["tests"])
    eg_ids = [c.id + ":eg" for c in plan.candidates]
    diff_ids = [c.id + ":diff:" + s for c in plan.candidates for s in c.symbols]
    stability_ids = [c.id + ":" + k for c in plan.candidates for k in ("cusum", "wald")]
    eg, differences, stability = adjust(tests, eg_ids), adjust(tests, diff_ids), adjust(tests, stability_ids)
    by = adjust(tests, eg_ids, "fdr_by")
    permits = {}
    for c in plan.candidates:
        reasons = []
        meta = eligibility.get(c.id, Eligibility())
        if (
            meta.status != "verified"
            or meta.known_at > view.ts
            or meta.source != "synthetic_metadata_fixture"
        ):
            reasons.append("point_in_time_eligibility_unknown")
        if any(tests[c.id + ":level:" + s]["pvalue"] <= config()["alpha"] for s in c.symbols):
            reasons.append("levels_not_compatible_with_I1_screen")
        if not all(differences[c.id + ":diff:" + s]["reject_null"] for s in c.symbols):
            reasons.append("differences_unit_root_not_rejected")
        if not eg[c.id + ":eg"]["reject_null"]:
            reasons.append("no_cointegration_not_rejected_after_Holm")
        if any(stability[c.id + ":" + k]["reject_null"] for k in ("cusum", "wald")):
            reasons.append("relationship_change_detected")
        if analyses[c.id]["half_life_days"] is None:
            reasons.append("half_life_unavailable")
        permits[c.id] = {
            "status": "rejected" if reasons else "qualified",
            "reasons": reasons,
            "source": "synthetic_statistical_screen_only",
            "symbols": list(c.symbols),
        }
    result = {
        "status": "complete",
        "origin": "synthetic_test",
        "decision_ts": view.ts,
        "plan": asdict(plan),
        "plan_sha256": digest(asdict(plan)),
        "analyses": analyses,
        "eligibility": {c.id: asdict(eligibility.get(c.id, Eligibility())) for c in plan.candidates},
        "adjustments": {
            "EG_Holm": eg,
            "difference_ADF_Holm": differences,
            "return_stability_Holm": stability,
            "EG_BY_diagnostic_not_license": by,
        },
        "permits": permits,
    }
    result["family_sha256"] = digest(result)
    return result


def evaluate_family(view, plan, eligibility, origin=None):
    if origin != "synthetic_test":
        raise Rejected("real_statistics_disabled", "M3B does not authorize real-data tests or qualification")
    dependencies()
    plan.validate()
    analyses, failures = {}, {}
    for c in plan.candidates:
        try:
            analyses[c.id] = analyze(view, plan, c)
        except (Rejected, ValueError, Warning, np.linalg.LinAlgError) as exc:
            failures[c.id] = {
                "code": getattr(exc, "code", "statistical_failure"),
                "error_type": type(exc).__name__,
            }
    if failures:
        return {
            "status": "unavailable",
            "plan_sha256": digest(asdict(plan)),
            "analyses": analyses,
            "failures": failures,
            "permits": {},
            "reason": "complete_family_unavailable_no_pvalues_imputed",
        }
    return decide_family(view, plan, analyses, eligibility, origin=origin)


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """Travels with each screened model; never consumes generator truth."""

    candidate_id: str
    decision_ts: int
    plan_sha256: str
    family_sha256: str
    training_sha256: str
    origin: str = field(default="synthetic_test", init=False)
    source: str = field(default="synthetic_statistical_screen_only", init=False)
    screen_only: bool = field(default=True, init=False)
    allowed_use: str = field(default="synthetic_calculation", init=False)
    limitations: str = field(
        default="Finite-sample screening can be wrong; no real qualification, trading, profit, capacity or deployment evidence",
        init=False,
    )

    def require_use(self, origin, use):
        if origin != self.origin or use != self.allowed_use:
            raise Rejected("real_model_use_disabled", self.limitations)


@dataclass(frozen=True)
class ScreenedPriceModel(Model):
    provenance: ModelProvenance = field(kw_only=True)

    def zscore(self, prices, *, origin="synthetic_test", use="synthetic_calculation"):
        self.provenance.require_use(origin, use)
        return super().zscore(prices)


@dataclass(frozen=True, slots=True)
class ReturnModel:
    symbols: tuple[str, ...]
    intercept: float
    beta: tuple[float, ...]
    mean: float
    sd: float
    provenance: ModelProvenance = field(kw_only=True)

    def residual(self, current, previous, *, origin="synthetic_test", use="synthetic_calculation"):
        self.provenance.require_use(origin, use)
        now, before = logs(current, self.symbols), logs(previous, self.symbols)
        dy = [a - b for a, b in zip(now, before, strict=True)]
        residual = (
            dy[0] - self.intercept - math.fsum(b * value for b, value in zip(self.beta, dy[1:], strict=True))
        )
        return {
            "relative_return_residual": residual,
            "standardized_return_residual": (residual - self.mean) / self.sd,
        }


def qualified_models(view, plan, batch, candidate_id, eligibility):
    if (
        batch.get("status") != "complete"
        or batch.get("origin") != "synthetic_test"
        or batch.get("decision_ts") != view.ts
        or batch.get("plan_sha256") != digest(asdict(plan))
    ):
        raise Rejected("qualification_unknown", "Complete, same-time, prespecified synthetic family required")
    copy = {k: v for k, v in batch.items() if k != "family_sha256"}
    if digest(copy) != batch["family_sha256"]:
        raise Rejected("family_mismatch", "Family evidence changed after screening")
    candidate = next((c for c in plan.candidates if c.id == candidate_id), None)
    if candidate is None or batch["permits"][candidate_id]["status"] != "qualified":
        raise Rejected("qualification_unknown", "Unknown/rejected candidate cannot obtain a model")
    if asdict(eligibility) != batch["eligibility"][candidate_id]:
        raise Rejected("eligibility_mismatch", "Point-in-time metadata changed")
    for member in plan.candidates:
        _, checked = window_data(view, member, plan)
        if checked["training_sha256"] != batch["analyses"][member.id]["training_sha256"]:
            raise Rejected("training_mismatch", "Permit binds every member of the complete family")
    _, sample = window_data(view, candidate, plan)
    analysis = batch["analyses"][candidate_id]
    if sample["training_sha256"] != analysis["training_sha256"]:
        raise Rejected("training_mismatch", "Permit is bound to the actual historical input digest")
    pm, rm = analysis["price_model"], analysis["return_model"]
    provenance = ModelProvenance(
        candidate_id, view.ts, batch["plan_sha256"], batch["family_sha256"], sample["training_sha256"]
    )
    price = ScreenedPriceModel(
        candidate.symbols,
        pm["intercept"],
        tuple(pm["beta"]),
        pm["mean"],
        pm["sd"],
        sample["train_start"],
        sample["train_end"],
        view.ts,
        analysis["phi"],
        analysis["half_life_days"],
        provenance=provenance,
    )
    returns = ReturnModel(
        candidate.symbols, rm["intercept"], tuple(rm["beta"]), rm["mean"], rm["sd"], provenance=provenance
    )
    return price, returns

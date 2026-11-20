"""Full candidate-by-configuration synthetic screening using frozen M3B statistical methods."""

import math

from ..engine import Rejected
from ..execution_audit import canonical, sha
from .contract import protocol


def screen(view, family, *, origin):
    if origin != "synthetic_test":
        raise Rejected("real_statistics_disabled", "No real experiment statistics before preregistration")
    from ..statistics.core import Candidate, FamilyPlan, analyze, dependencies

    dependencies()
    plan = FamilyPlan(
        "experiment_R1_frozen_pool", tuple(Candidate(c["id"], tuple(c["symbols"])) for c in family)
    )
    plan.validate()
    analyses, failures = {}, {}
    for c in plan.candidates:
        try:
            analyses[c.id] = analyze(view, plan, c)
        except (Rejected, ValueError, Warning) as exc:
            failures[c.id] = {
                "reason": getattr(exc, "code", "statistics_unavailable"),
                "type": type(exc).__name__,
            }
    return bind_family(analyses, failures, family, view.ts)


def bind_family(analyses, failures, family, decision_ts):
    """All test IDs retained. Missing tests never acquire p=1 or disappear from multiplicity."""
    from ..statistics.core import Candidate, adjust, test_ids

    configs = protocol()["configs"]
    expected = {c["id"] for c in family}
    if set(analyses) | set(failures) != expected or set(analyses) & set(failures):
        raise Rejected("incomplete_test_family", "Every candidate needs analysis or explicit failure")
    for c in family:
        row = analyses.get(c["id"])
        if row and (
            set(row["tests"]) != set(test_ids(Candidate(c["id"], tuple(c["symbols"]))))
            or row["decision_ts"] != decision_ts
            or tuple(row["symbols"]) != tuple(c["symbols"])
        ):
            raise Rejected("incomplete_test_family", "Every statistic and same-time binding required")
        if row:
            for test in row["tests"].values():
                if (
                    not math.isfinite(test["pvalue"])
                    or not 0 <= test["pvalue"] <= 1
                    or not math.isfinite(test["statistic"])
                ):
                    raise Rejected("invalid_statistic", "No nonfinite or out-of-range test values")
    expanded, unavailable = {}, []
    for c in family:
        for cfg in configs:
            prefix = cfg["id"] + "/" + c["id"]
            if c["id"] in failures:
                unavailable += [
                    prefix + "/" + k.split(":", 1)[1]
                    for k in test_ids(Candidate(c["id"], tuple(c["symbols"])))
                ]
            else:
                expanded.update(
                    {prefix + "/" + k.split(":", 1)[1]: v for k, v in analyses[c["id"]]["tests"].items()}
                )
    if unavailable:
        holm, by = {}, {}
    else:
        # R2: level ADF is diagnostic; primary EG+difference+stability family is never survivor-resized.
        primary_ids = sorted(k for k in expanded if "/level:" not in k)
        by_ids = sorted(k for k in expanded if k.endswith("/eg"))
        holm = adjust(expanded, primary_ids)
        by = adjust(expanded, by_ids, "fdr_by")
    permits = {}
    for c in family:
        for cfg in configs:
            key = cfg["id"] + "/" + c["id"]
            reasons = []
            if unavailable:
                reasons.append("complete_family_unavailable")
            else:
                row = analyses[c["id"]]
                if not holm[key + "/eg"]["reject_null"]:
                    reasons.append("EG_not_rejected_after_full_family_Holm")
                if any(row["tests"][c["id"] + ":level:" + s]["pvalue"] <= 0.05 for s in c["symbols"]):
                    reasons.append("levels_not_I1_compatible")
                if not all(holm[key + "/diff:" + s]["reject_null"] for s in c["symbols"]):
                    reasons.append("differences_not_stationary_after_Holm")
                # Retain stricter unadjusted break detection too: multiplicity must not hide instability.
                if any(row["tests"][c["id"] + ":" + k]["pvalue"] <= 0.05 for k in ("cusum", "wald")):
                    reasons.append("relationship_change_detected")
                if row["half_life_days"] is None:
                    reasons.append("half_life_unknown")
            permits[key] = {
                "status": "rejected" if reasons else "qualified",
                "reasons": reasons,
                "origin": "synthetic_test",
                "source": "synthetic_statistical_screen_only",
            }
    result = {
        "origin": "synthetic_test",
        "status": "unavailable" if unavailable else "complete",
        "decision_ts": decision_ts,
        "family": family,
        "config_ids": [c["id"] for c in configs],
        "analyses": analyses,
        "failures": failures,
        "unavailable_test_ids": unavailable,
        "tests": expanded,
        "Holm_FWER": holm,
        "BY_FDR_diagnostic": by,
        "permits": permits,
        "plan": asdict_plan(family, configs),
    }
    result["family_sha256"] = sha(canonical(result))
    return result


def asdict_plan(family, configs):
    return {
        "candidates": family,
        "configurations": configs,
        "registration": protocol()["registration_sha256"],
    }


def check_batch(batch):
    if batch.get("origin") != "synthetic_test" or sha(
        canonical({k: v for k, v in batch.items() if k != "family_sha256"})
    ) != batch.get("family_sha256"):
        raise Rejected("family_mismatch", "Complete family must remain bound and synthetic")

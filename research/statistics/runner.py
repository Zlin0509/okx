"""Versioned synthetic reports and tolerant cross-platform numerical verification."""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..engine import Rejected
from ..execution_audit import append, canonical
from ..synthetic_guard import install
from .core import dependencies, digest, evaluate_family, qualified_models
from .fixtures import asof, family, synthetic_series

ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / "research/M3B_SOURCE_FREEZE.json"
EXPECTED = ROOT / "research/M3B_EXPECTED.json"
RESULTS = ROOT / "research/results/m3b"


def sha(body):
    return hashlib.sha256(body).hexdigest()


def reproduce():
    series = synthetic_series()
    view = asof(series)
    plan, eligibility = family()
    batch = evaluate_family(view, plan, eligibility, origin="synthetic_test")
    if batch["status"] != "complete":
        raise AssertionError(batch)
    models = {}
    for c in plan.candidates:
        if batch["permits"][c.id]["status"] == "qualified":
            price, returns = qualified_models(view, plan, batch, c.id, eligibility[c.id])
            models[c.id] = {
                "price_z": price.zscore(view.observations[view.ts].prices),
                **returns.residual(
                    view.observations[view.ts].prices, view.observations[view.ts - 86400000].prices
                ),
            }
    truth = {
        "pair": True,
        "basket": True,
        "independent": False,
        "rw_residual": False,
        "break": False,
        "stationary_levels": False,
    }
    observed = {name: row["status"] == "qualified" for name, row in batch["permits"].items()}
    return {
        "label": "SYNTHETIC STATISTICS ONLY — no strategy, real qualification or profit evidence",
        "batch": batch,
        "models": models,
        "generator_truth_price_relation": truth,
        "false_positives": sorted(name for name in truth if observed[name] and not truth[name]),
        "false_negatives": sorted(name for name in truth if truth[name] and not observed[name]),
        "limit": "Finite-sample errors are retained, not repaired by changing seeds or thresholds",
    }


def compare(actual, expected, path="result"):
    if isinstance(expected, float):
        if (
            type(actual) not in (float, int)
            or not math.isfinite(actual)
            or not math.isclose(actual, expected, rel_tol=1e-7, abs_tol=1e-8)
        ):
            raise Rejected("numerical_mismatch", path)
    elif isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise Rejected("schema_mismatch", path)
        for key in expected:
            compare(actual[key], expected[key], path + "/" + key)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise Rejected("schema_mismatch", path)
        for i, (a, b) in enumerate(zip(actual, expected, strict=True)):
            compare(a, b, path + "/" + str(i))
    elif type(actual) is not type(expected) or actual != expected:
        raise Rejected("discrete_mismatch", path)


def numerical_reference(value):
    # Content hashes over floating statistics differ with BLAS/Python. Compare those numbers directly.
    if isinstance(value, dict):
        return {k: numerical_reference(v) for k, v in value.items() if k != "family_sha256"}
    if isinstance(value, list):
        return [numerical_reference(v) for v in value]
    return value


def verify_sources():
    for name in ("M3B_PREREGISTRATION.json", "M3B_SOURCE_FREEZE.json"):
        spec = json.loads((ROOT / "research" / name).read_text(encoding="utf-8"))
        for path, expected in spec["sha256"].items():
            if sha((ROOT / path).read_bytes()) != expected:
                raise Rejected("freeze_mismatch", path)


def perform(action):
    verify_sources()
    result = reproduce()
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    compare(numerical_reference(result), numerical_reference(expected))
    content = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if action == "write":
        if RESULTS.exists():
            raise Rejected("frozen_directory_exists", "M3B never overwrites a captured result directory")
        RESULTS.mkdir(parents=True)
        (RESULTS / "statistics.json").write_text(content, encoding="utf-8")
    elif RESULTS.exists():
        compare(
            numerical_reference(result),
            numerical_reference(json.loads((RESULTS / "statistics.json").read_text(encoding="utf-8"))),
        )
    discrete = {
        "permits": result["batch"]["permits"],
        "false_positives": result["false_positives"],
        "false_negatives": result["false_negatives"],
    }
    return {
        "scope": "synthetic_statistics_only",
        "decision_sha256": digest(discrete),
        "statistics_sha256": sha(content.encode()),
        "dependency_versions": dependencies(),
        "false_positives": result["false_positives"],
        "artifacts_present": RESULTS.exists(),
    }


def main(guard_counts=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "write", "real"))
    args = parser.parse_args()
    try:
        if args.action == "real":
            raise Rejected("real_statistics_disabled", "No real data or research qualification in M3B")
        guard = install() if guard_counts is None else guard_counts
        start = datetime.now(UTC).isoformat()
        common = {
            "run_id": str(uuid.uuid4()),
            "operation": "m3b-" + args.action,
            "started_at_utc": start,
            "python": sys.version.split()[0],
            "argv": list(sys.argv),
            "HEAD": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "source_freeze_sha256": sha(FREEZE.read_bytes()),
            "thread_configuration": {
                k: os.environ.get(k) for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
            },
        }
        log = ROOT / "research/runtime/m3b.jsonl"
        append(log, {**common, "event": "start"})
        try:
            result = {**perform(args.action), "guard_counts": dict(guard)}
            if any(guard.values()):
                raise Rejected("guard_violation", "Guard rejected an unexpected operation")
        except Exception as exc:
            failure = {"code": getattr(exc, "code", None), "error_type": type(exc).__name__}
            append(
                log,
                {
                    **common,
                    "event": "finish",
                    "status": "failed",
                    "finished_at_utc": datetime.now(UTC).isoformat(),
                    "result": failure,
                    "result_sha256": sha(canonical(failure)),
                },
            )
            raise
        append(
            log,
            {
                **common,
                "event": "finish",
                "status": "passed",
                "finished_at_utc": datetime.now(UTC).isoformat(),
                "result": result,
                "result_sha256": sha(canonical(result)),
            },
        )
        print(json.dumps(result, sort_keys=True))
    except Rejected as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

"""Registration, fail-closed real entry, validation-only selection and local experiment journal."""

import fcntl
import json
import math
from datetime import UTC, datetime
from pathlib import Path

from ..engine import Rejected
from ..execution_audit import append, canonical, read_log, sha

ROOT = Path(__file__).resolve().parent
REGISTRATION_SHA = "1b85d581f5605714bef3df66b3fd6da1fdf30449dba68e93152d8c3c228303f2"


def protocol():
    if sha((ROOT / "ACCEPTANCE_R1.json").read_bytes()) != REGISTRATION_SHA:
        raise Rejected("registration_mismatch", "Leader registration must remain byte-identical")
    p = json.loads((ROOT / "PROTOCOL_R1.json").read_text())
    if p["registration_sha256"] != REGISTRATION_SHA or len(p["configs"]) != 6:
        raise Rejected("protocol_mismatch", "Exactly six registered configurations required")
    return p


def real_preflight(artifacts=None):
    artifacts = artifacts or {}
    reasons = ["real_execution_disabled_pending_independent_preregistration"]
    reasons += [
        "missing_or_unreviewed:" + k
        for k in protocol()["real_required_artifacts"]
        if artifacts.get(k, {}).get("reviewed") is not True
    ]
    return {
        "status": "refused",
        "reasons": reasons,
        "real_returns": None,
        "test_integrity": "unverified",
        "quarantine": ["2026-08-14", "2026-10-31"],
        "registration_sha256": REGISTRATION_SHA,
    }


def select_validation(rows):
    """Synthetic/mocked validation records only. Never accepts test metrics or selects using test."""
    ids = {c["id"] for c in protocol()["configs"]}
    if set(rows) != ids:
        raise Rejected("incomplete_configuration_family", "Keep all six, including rejected members")
    for row in rows.values():
        if (
            row.get("origin") != "synthetic_test"
            or row.get("stage") != "validation"
            or set(row)
            != {"origin", "stage", "status", "net_cagr", "net_sharpe", "turnover", "data_risk_pass"}
        ):
            raise Rejected("validation_only", "No test fields, generator truth, or real winner selection")
        if row["status"] == "available" and (
            any(
                type(row[k]) not in (float, int) or not math.isfinite(row[k])
                for k in ("net_cagr", "net_sharpe", "turnover")
            )
            or row["turnover"] < 0
        ):
            raise Rejected("undefined_metric", "Finite validation metrics required")
        if row["status"] not in {"available", "unavailable", "failed"}:
            raise Rejected("invalid_status", "Explicit unavailable and failed records required")
    neighbours = ("window28", "window112", "entry15", "entry25")

    def good(k):
        return (
            rows[k]["status"] == "available" and rows[k]["data_risk_pass"] is True and rows[k]["net_cagr"] > 0
        )

    if sum(good(k) for k in neighbours) < 3:
        raise Rejected("neighbour_robustness_failed", "Secondary cannot replace failed primary robustness")
    choices = [k for k in ids if good(k)]
    if not choices:
        raise Rejected("no_qualified_validation_configuration", "Keep all failures")
    return sorted(choices, key=lambda k: (-rows[k]["net_sharpe"], rows[k]["turnover"], k != "primary", k))[0]


class Journal:
    """Local file lock + chained append log; no global anti-rerun or external attestation claim."""

    def __init__(self, path):
        self.path = Path(path)

    def record(self, **row):
        append(
            self.path,
            {
                "event": "experiment",
                "at_utc": datetime.now(UTC).isoformat(),
                "registration_sha256": REGISTRATION_SHA,
                **row,
            },
        )

    def reserve(self, config, stage, binding, *, origin):
        if origin != "synthetic_test":
            raise Rejected("real_execution_disabled", "Local synthetic journal is not real authorization")
        if config not in {c["id"] for c in protocol()["configs"]} or stage not in {
            "training_validation",
            "confirmation",
        }:
            raise Rejected("unregistered_experiment", "No seventh configuration or unknown stage")
        if set(binding) != {"data_sha256", "family_sha256", "source_sha256", "dates_sha256"} or any(
            not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
            for v in binding.values()
        ):
            raise Rejected(
                "missing_experiment_binding", "Exact data/family/source/date SHA-256 bindings required"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entries = read_log(self.path)
            reservations = [e for e in entries if e.get("action") == "reserve"]
            if stage == "confirmation":
                selected = [e for e in entries if e.get("action") == "selection"]
                if not selected or selected[-1]["config"] != config:
                    raise Rejected(
                        "validation_selection_required",
                        "Only the validation-selected configuration may confirm",
                    )
            if stage == "confirmation" and any(e["stage"] == "confirmation" for e in reservations):
                raise Rejected("test_already_consumed", "Failure does not restore the one confirmation slot")
            if any(e["config"] == config and e["stage"] == stage for e in reservations):
                raise Rejected(
                    "experiment_already_reserved", "Use exact frozen result verification, not a new run"
                )
            if len({e["config"] for e in reservations} | {config}) > 6:
                raise Rejected("experiment_budget_exhausted", "Six configurations maximum")
            token = sha(canonical([config, stage, binding, len(entries)]))
            self.record(
                action="reserve", token=token, config=config, stage=stage, origin=origin, binding=binding
            )
            return token

    def finish(self, token, result, status):
        if status not in {"passed", "failed", "refused"}:
            raise Rejected("invalid_status", "Retain failures and refusals")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entries = read_log(self.path)
            if not any(e.get("action") == "reserve" and e.get("token") == token for e in entries) or any(
                e.get("action") == "result" and e.get("token") == token for e in entries
            ):
                raise Rejected("invalid_experiment_token", "One finish per recorded reservation")
            self.record(
                action="result",
                token=token,
                status=status,
                result=result,
                result_sha256=sha(canonical(result)),
            )

    def select(self, rows):
        config = select_validation(rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if any(e.get("stage") == "confirmation" for e in read_log(self.path)):
                raise Rejected("test_already_consumed", "No selection changes after test reservation")
            self.record(
                action="selection",
                config=config,
                validation_sha256=sha(canonical(rows)),
                origin="synthetic_test",
            )
        return config

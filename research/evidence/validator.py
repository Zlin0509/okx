"""Structured preflight diagnostics with no route to strategy execution."""

import json
from pathlib import Path

from ..execution_audit import canonical, sha
from ..experiments.contract import DECISIONS_SHA, REGISTRATION_SHA, protocol
from . import coverage
from .calendar import calendar, exposures, missing_coverage
from .common import INPUT_ERRORS, Findings, check, day, instant
from .funding import settlement
from .intake import anchored, open_root, sources, witness

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "research/experiments"
R3_FREEZE_SHA = "52f81519b8de48bec1de6c020ed1037450c97cf439e5e3ffb4e7d2b3bf7f3b56"
KNOWN_EXPOSURES_SHA = "3ddaea974a1341beddb0648b0dd3313ff203d1c605c79de80d7fabe47039c903"
UNIVERSE = "historical_universe_manifest"
FAMILY = "complete_candidate_configuration_family"
DATES = "exact_eligible_dates"
EXPOSURE = "prior_exposure_audit"
FUNDING = "actual_funding_calendar_and_marks"
SPOT = "spot_baseline_prices"
VENUE = "venue_constraints_and_capacity"
FREEZE = "source_and_dependency_freeze"
APPROVAL = "independent_preregistration_approval"
ARTIFACTS = (UNIVERSE, FAMILY, DATES, EXPOSURE, FUNDING, SPOT, VENUE, FREEZE, APPROVAL)
GUARD_REASON = "real_execution_disabled_pending_independent_preregistration"


def policy_anchors():
    return {
        "registration_sha256": REGISTRATION_SHA,
        "decisions_sha256": DECISIONS_SHA,
        "source_freeze_sha256": R3_FREEZE_SHA,
        "dependency_lock_sha256": sha((ROOT / "research/statistics/requirements.lock").read_bytes()),
    }


def freeze(body, root, policy):
    check(root["policy"] == policy_anchors(), "dossier_policy_anchor_mismatch")
    check(body["policy"] == root["policy"], "source_dependency_freeze_binding_mismatch")
    check(
        body["configuration_ids"] == [row["id"] for row in policy["configs"]],
        "six_configuration_budget_mismatch",
    )
    raw = (EXPERIMENT / "SOURCE_FREEZE_R3_V3.json").read_bytes()
    check(sha(raw) == R3_FREEZE_SHA, "reviewed_base_freeze_changed")
    for path, digest in json.loads(raw)["sha256"].items():
        check(sha((ROOT / path).read_bytes()) == digest, "reviewed_base_source_changed", path)


def _exposure(body, cal, as_of, src, directory, origin, findings):
    exposures(body, cal, as_of, src, findings, EXPOSURE)
    if origin == "synthetic_test":
        return
    check(body["prior_exposure_manifest_sha256"] == KNOWN_EXPOSURES_SHA, "known_exposure_lineage_missing")
    known = anchored(directory, "known-exposure-r3.json", KNOWN_EXPOSURES_SHA)
    seen = {day(row["published_at_reported_utc"][:10]) for row in known["announcement_index"]["records"]}
    imported = [
        (day(row["first_date"]), day(row["last_date"]))
        for row in body["exposures"]
        if row["kind"] == "historical_metadata" and row.get("first_date") and row.get("last_date")
    ]
    eligible = set(cal["test"])
    for d in sorted(seen):
        if not any(start <= d <= end for start, end in imported):
            findings.add(EXPOSURE, "known_metadata_exposure_not_carried_forward", d.isoformat())
            if d in eligible:
                findings.add(EXPOSURE, "eligible_date_has_known_r3_metadata_exposure", d.isoformat())


def approval(body, root, as_of, src):
    reviewer = body["reviewer_id"]
    check(isinstance(reviewer, str) and bool(reviewer.strip()), "missing_independent_reviewer")
    author = root["prepared_by"]
    check(
        isinstance(author, str) and bool(author.strip()) and reviewer != author,
        "reviewer_not_distinct_from_preparer",
    )
    recorded = witness(src, body["source_id"], "independent_review_receipt")
    check(as_of <= instant(body["reviewed_at"]) <= recorded, "review_does_not_follow_data_cutoff")
    check(body["decision"] == "approved_for_registered_scope", "no_independent_acceptance_receipt")
    # Bind all evidence member bytes except the receipt itself, and the semantic root metadata.
    binding = {k: v for k, v in root.items() if k != "files"} | {
        "files": {k: v for k, v in root["files"].items() if k != APPROVAL}
    }
    check(body["evidence_binding_sha256"] == sha(canonical(binding)), "review_receipt_binding_mismatch")


def _verify_validator_sources():
    body = Path(__file__).with_name("EVIDENCE_SOURCE_FREEZE_V3.json").read_bytes()
    for path, digest in json.loads(body)["sha256"].items():
        check(sha((ROOT / path).read_bytes()) == digest, "validator_source_hash_mismatch", path)
    return sha(body)


def audit(directory, expected_sha256):
    findings = Findings()
    result = {
        "schema": "zlin11-evidence-audit-v1",
        "status": "evidence_incomplete",
        "dossier_sha256": expected_sha256,
        "origin": None,
        "real_execution_allowed": False,
        "real_returns": None,
        "blocked_execution_reason": GUARD_REASON,
        "independent_historical_certification": None,
        "verified_source_count": 0,
        "calendar": None,
        "settlements": None,
    }

    def attempt(artifact, callback, *args):
        try:
            return callback(*args)
        except (*INPUT_ERRORS, OSError) as exc:
            findings.invalid(artifact, exc)
            return None

    def gate(name, dependencies, callback):
        if name not in materials:
            return None
        missing = [k for k, present in dependencies.items() if present is None]
        if missing:
            for key in missing:
                findings.add(name, "dependency_unavailable", key)
            return None
        return attempt(name, callback, materials[name])

    manifest_sha = attempt("validator", _verify_validator_sources)
    result["validator_source_freeze_sha256"] = manifest_sha
    opened = (
        attempt("dossier", open_root, Path(directory), expected_sha256, ARTIFACTS, findings)
        if manifest_sha
        else None
    )
    materials = {}
    if opened is not None:
        root, directory, as_of, materials, index = opened
        result["origin"] = root["origin"]
        src = attempt("sources", sources, directory, index, root["origin"], as_of, findings) or {}
        result["verified_source_count"] = len(src)

        def registered_policy():
            frozen = protocol()
            check(
                tuple(frozen["real_required_artifacts"]) == ARTIFACTS, "required_evidence_contract_mismatch"
            )
            return frozen

        p = attempt(FREEZE, registered_policy)
        gate(FREEZE, {"protocol": p}, lambda body: freeze(body, root, p))
        # The frozen R1/R2 bytes are checked by protocol() before any date join.
        registration = None if p is None else json.loads((EXPERIMENT / "ACCEPTANCE_R1.json").read_bytes())
        cal = gate(
            DATES,
            {"registration": registration},
            lambda body: calendar(body, registration, as_of, src, findings, DATES),
        )
        u = gate(UNIVERSE, {}, lambda body: coverage.universe(body, src))
        req = gate(
            FAMILY,
            {DATES: cal, UNIVERSE: u, "protocol": p},
            lambda body: coverage.decisions(body, cal, u, src, p, findings, FAMILY),
        )
        gate(
            EXPOSURE,
            {DATES: cal},
            lambda body: _exposure(body, cal, as_of, src, directory, root["origin"], findings),
        )
        if cal:
            result["calendar"] = {f"{k}_days": len(cal[k]) for k in ("train", "validation", "test")}
            result["calendar"].update(
                {
                    "test_window": cal["test_window"],
                    "first_test_date": cal["test"][0].isoformat(),
                    "last_test_date": cal["test"][-1].isoformat(),
                }
            )
        if req:
            missing_coverage(
                u["daily_market_coverage"],
                req["markets"],
                findings,
                UNIVERSE,
                "missing_perpetual_open_close_day",
            )
        result["settlements"] = gate(
            FUNDING,
            {FAMILY: req},
            lambda body: settlement(body, req["cost_days"], src, as_of, findings, FUNDING),
        )
        gate(
            SPOT,
            {DATES: cal, UNIVERSE: u, FAMILY: req},
            lambda body: coverage.spot(body, cal, u, req, src, findings, SPOT),
        )
        gate(VENUE, {FAMILY: req}, lambda body: coverage.venue(body, req, src, findings, VENUE))
        gate(APPROVAL, {}, lambda body: approval(body, root, as_of, src))

    for name in ARTIFACTS:
        if opened is None:
            findings.add(name, "root_anchor_unavailable")
    result["findings"] = findings.result()
    flagged = {entry["artifact"] for entry in result["findings"]}
    result["materials"] = {
        name: "validated" if name in materials and name not in flagged else "unavailable"
        for name in ARTIFACTS
    }
    if not result["findings"]:
        result["status"] = (
            "synthetic_evidence_consistent"
            if result["origin"] == "synthetic_test"
            else "historical_manifest_checks_passed_review_required"
        )
    return result

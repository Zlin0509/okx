"""Verify detached and member hashes before using any local JSON evidence."""

import os
from pathlib import Path

from ..execution_audit import sha
from .common import HEX, INPUT_ERRORS, SAFE_ID, Invalid, check, decode, instant

PROTECTED = {"snapshots", "results", "audit", "ws_data", "ws_results", "ws_audit", "experiment_audit"}
SOURCE_KINDS = {
    "eligible_calendar",
    "historical_instrument_events",
    "pre_statistics_registry",
    "exposure_ledger",
    "settlement_schedule",
    "settled_funding_records",
    "exact_settlement_marks",
    "spot_baseline_calendar",
    "venue_and_turnover_calendar",
    "independent_review_receipt",
}


def _safe_bytes(directory, name):
    path = directory / name
    # Schema fixes all paths. Symlinks cannot redirect any claimed evidence member.
    check(not path.is_symlink() and path.parent.resolve() == path.parent, "unsafe_evidence_path", name)
    if not path.is_file():
        raise Invalid("missing_evidence_file", name)
    return path.read_bytes()


def anchored(directory, name, digest):
    check(isinstance(digest, str) and bool(HEX.fullmatch(digest)), "invalid_sha256", name)
    try:
        raw = _safe_bytes(directory, name)
    except OSError as exc:
        raise Invalid("evidence_file_unreadable", name) from exc
    check(sha(raw) == digest, "sha256_mismatch", name)
    body = decode(raw)
    check(isinstance(body, dict), "expected_json_object", name)
    return body


def open_root(directory, expected, artifacts, findings):
    check(not directory.is_symlink(), "unsafe_evidence_path", "dossier directory")
    directory = Path(os.path.abspath(directory))
    check(directory == directory.resolve(), "unsafe_evidence_path", "dossier directory")
    check(
        not any(
            a == "research" and b in PROTECTED
            for a, b in zip(directory.parts, directory.parts[1:], strict=False)
        ),
        "protected_research_path",
        "dossier directory",
    )
    root = anchored(directory, "dossier.json", expected)
    check(root["schema"] == "zlin11-evidence-v1", "unsupported_dossier_schema")
    origin = root["origin"]
    check(origin in {"synthetic_test", "historical_evidence"}, "invalid_dossier_origin")
    as_of = instant(root["as_of_utc"])
    hashes = root["files"]
    check(isinstance(hashes, dict), "expected_file_index")
    source_index, materials = {}, {}
    for name in sorted(set(hashes) - set(artifacts) - {"sources"}):
        findings.add("dossier", "unexpected_member", name)
    for name in (*artifacts, "sources"):
        try:
            if name not in hashes:
                raise Invalid("missing_material_in_file_index", name)
            body = anchored(directory, name + ".json", hashes[name])
            if name == "sources":
                source_index = body
            else:
                check(body.get("origin") == origin, "material_origin_mismatch", name)
                materials[name] = body
        except INPUT_ERRORS as exc:
            findings.invalid(name, exc)
    return root, directory, as_of, materials, source_index


def sources(directory, source_index, origin, as_of, findings):
    out, seen = {}, set()
    for row in source_index.get("sources", []):
        source_id = ""
        try:
            source_id = row["id"]
            check(isinstance(source_id, str) and bool(SAFE_ID.fullmatch(source_id)), "invalid_source_id")
            if source_id in seen:
                out.pop(source_id, None)
                raise Invalid("duplicate_source_id", source_id)
            seen.add(source_id)
            kind = row["kind"]
            check(kind in SOURCE_KINDS, "unsupported_source_kind", source_id)
            doc = anchored(directory, f"sources/{source_id}.json", row["sha256"])
            check(
                doc["id"] == source_id and doc["kind"] == kind and doc["origin"] == origin,
                "source_identity_or_role_mismatch",
                source_id,
            )
            captured = instant(doc["captured_at"])
            check(
                kind == "independent_review_receipt" or captured <= as_of,
                "source_captured_after_audit_cutoff",
                source_id,
            )
            check(
                isinstance(doc["reference"], str) and bool(doc["reference"].strip()),
                "missing_source_reference",
                source_id,
            )
            out[source_id] = (kind, captured)
        except INPUT_ERRORS as exc:
            findings.invalid("sources", exc)
    if not out:
        findings.add("sources", "no_verified_source_witnesses")
    return out


def witness(source_index, source_id, kind):
    check(source_id in source_index, "unverified_source_reference", source_id)
    actual, captured = source_index[source_id]
    check(actual == kind, "wrong_source_role", source_id)
    return captured

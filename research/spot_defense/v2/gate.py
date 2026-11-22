"""Fail-closed isolation review and evaluation authorization."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ..protocol import CHECK_HOUR, at
from .calendar import END, INITIAL_RESERVE_SHA256, PREREG_SHA256, V1_CONFIG_SHA256, bounds
from .evidence import checksum, digest, fields, inspect_manifest, instant, read_evidence, unpack


@dataclass(frozen=True)
class ReviewAuthority:
    reviewer_id: str
    public_key: bytes
    key_sha256: str
    revision: str
    implementation_sha256: str
    commands_sha256: str
    state_directory: Path


# An independent launcher must pin this authority in the supplemental freeze.
# No CLI flag can install trust or alter the current clock.
AUTHORITY: ReviewAuthority | None = None


def source_sha256():
    folder = Path(__file__).resolve().parent
    sources = [
        *folder.glob("*.py"),
        *(
            folder.parent / name
            for name in (
                "protocol.py",
                "data.py",
                "collect.py",
                "engine.py",
                "run.py",
                "stress.py",
            )
        ),
    ]
    return digest(
        "".join(f"{p.parent.name}/{p.name} {digest(p.read_bytes())}\n" for p in sorted(sources)).encode()
    )


def command_sha256():
    return digest(Path(__file__).with_name("commands.json").read_bytes())


def _verify(authority, body, signature):
    if digest(authority.public_key) != checksum(authority.key_sha256):
        raise ValueError("reviewer key pin mismatch")
    with TemporaryDirectory(prefix="zlin21-signature-") as work:
        root = Path(work)
        (root / "key").write_bytes(authority.public_key)
        (root / "body").write_bytes(body)
        (root / "signature").write_bytes(signature)
        result = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(root / "key"),
                "-rawin",
                "-in",
                str(root / "body"),
                "-sigfile",
                str(root / "signature"),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    if result.returncode:
        raise ValueError("invalid independent approval signature")


COMMON = "schema kind reviewer_id issued_at prereg_sha256 config_sha256 revision implementation_sha256 commands_sha256 manifest_sha256 audit_sha256"


def _approval(authority, body, signature, kind, now):
    _verify(authority, body, signature)
    obj = unpack(body)
    release_fields = " isolation_sha256 review_sha256" if kind == "release" else ""
    fields(obj, COMMON + release_fields)
    issued = instant(obj["issued_at"])
    if (
        type(obj["schema"]) is not int
        or obj["schema"] != 1
        or obj["kind"] != kind
        or obj["reviewer_id"] != authority.reviewer_id
        or obj["prereg_sha256"] != PREREG_SHA256
        or obj["config_sha256"] != V1_CONFIG_SHA256
        or obj["revision"] != authority.revision
        or obj["implementation_sha256"] != authority.implementation_sha256
        or obj["commands_sha256"] != authority.commands_sha256
        or not at(END, CHECK_HOUR) <= issued <= now
    ):
        raise ValueError("approval scope, identity or date mismatch")
    for key in ("manifest_sha256", "audit_sha256", *release_fields.split()):
        checksum(obj[key])
    return obj


def _check_audit(body, expected_sha256):
    if digest(body) != expected_sha256:
        raise ValueError("FUTURE_RESERVE audit digest mismatch")
    obj = unpack(body)
    fields(
        obj,
        "initial_reserve_sha256 bounds research_accesses reserve_log_sha256 run_log_sha256 prior_exposure_sha256",
    )
    if obj["initial_reserve_sha256"] != INITIAL_RESERVE_SHA256 or obj["bounds"] != bounds():
        raise ValueError("wrong exposure audit scope")
    if obj["research_accesses"] != []:
        raise ValueError("protected research exposure reported")
    for key in ("reserve_log_sha256", "run_log_sha256", "prior_exposure_sha256"):
        checksum(obj[key])


@dataclass(frozen=True)
class Approval:
    authority: ReviewAuthority
    manifest_sha256: str
    isolation_sha256: str
    review_sha256: str | None


def authorize(
    authority, now: datetime, isolation, isolation_signature, audit, *, release=None, release_signature=None
):
    if (release is None) != (release_signature is None):
        raise ValueError("release and its signature must be supplied together")
    if authority is None:
        raise ValueError("independent reviewer authority has not been frozen")
    if now < at(END, CHECK_HOUR):
        raise ValueError("protected window remains sealed")
    if (
        not authority.reviewer_id
        or not re.fullmatch(r"[a-f0-9]{40}", authority.revision)
        or authority.implementation_sha256 != source_sha256()
        or authority.commands_sha256 != command_sha256()
        or digest(Path(__file__).resolve().parent.parent.joinpath("protocol.py").read_bytes())
        != V1_CONFIG_SHA256
    ):
        raise ValueError("candidate or commands differ from the independent freeze")
    isolated = _approval(authority, isolation, isolation_signature, "isolation", now)
    _check_audit(audit, isolated["audit_sha256"])
    isolation_hash = digest(isolation)
    if release is None:
        return Approval(authority, isolated["manifest_sha256"], isolation_hash, None)
    approved = _approval(authority, release, release_signature, "release", now)
    if (
        approved["isolation_sha256"] != isolation_hash
        or any(approved[k] != isolated[k] for k in ("manifest_sha256", "audit_sha256"))
        or instant(approved["issued_at"]) < instant(isolated["issued_at"])
    ):
        raise ValueError("release does not match the isolation approval")
    return Approval(authority, isolated["manifest_sha256"], isolation_hash, approved["review_sha256"])


def validated_market(root, approval):
    manifest = inspect_manifest((root / "manifest.json").read_bytes(), expected_hash=approval.manifest_sha256)
    market = read_evidence(root, manifest)
    coverage = market.coverage()
    counts = {key: len(missing) for key, missing in coverage["missing"].items()}
    if counts != manifest["missing_counts"]:
        raise ValueError("actual coverage differs from the published manifest")
    return market, {
        "manifest_sha256": approval.manifest_sha256,
        "isolation_sha256": approval.isolation_sha256,
        "coverage": coverage,
    }


def report_bytes(report):
    return (json.dumps(report, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


def marker_path(approval):
    return approval.authority.state_directory / "zlin21-v2-evaluation"


def claim(approval):
    # The storage owner controls one durable directory for the entire v2 window.
    # Retaining the marker on failure prevents an automatic second reveal.
    if not approval.review_sha256:
        raise ValueError("release approval required")
    marker = marker_path(approval)
    with marker.open("x", encoding="ascii") as file:
        file.write(approval.review_sha256 + "\n")
        file.flush()
        os.fsync(file.fileno())
    directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return marker

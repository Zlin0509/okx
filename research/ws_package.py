"""Externally anchored archive import; reuse the unchanged peer read-only verifier."""

import io
import json
import runpy
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .engine import Rejected
from .execution_audit import sha

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "research/vendor/verify_okx_live_capture_readonly.py.txt"
VENDOR_SHA = "af22b85f4c08dcf7509f5d603dfeef2b2650c4c48ea2a3e60465a07b50da96fb"


def verifier():
    if sha(VENDOR.read_bytes()) != VENDOR_SHA:
        raise Rejected("verifier_source_mismatch", "Unchanged, pinned peer verifier required")
    # Execute only repository-owned pinned code, never a script supplied by the archive.
    return runpy.run_path(str(VENDOR), run_name="ws_readonly_verifier")


def safe_member(name):
    p = PurePosixPath(name)
    if not name or "\\" in name or p.is_absolute() or ".." in p.parts or str(p) != name:
        raise Rejected("unsafe_archive_member", name)


def read_capture(path, anchors):
    path = Path(path)
    blob = path.read_bytes()
    if sha(blob) != anchors["zip_sha256"]:
        raise Rejected("external_zip_sha256_mismatch", "Only the authorized complete archive is accepted")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = z.namelist()
            if len(set(names)) != len(names) or sum(i.file_size for i in z.infolist()) > 32_000_000:
                raise Rejected("invalid_archive", "Unique, bounded archive members required")
            for name in names:
                safe_member(name)
            members = {name: z.read(name) for name in names}
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise Rejected("invalid_archive", str(exc)) from exc
    raw = members.get(anchors["raw_member"])
    if raw is None:
        raise Rejected("raw_member_missing", anchors["raw_member"])
    if sha(raw) != anchors["raw_sha256"]:
        raise Rejected("external_raw_sha256_mismatch", "External raw anchor failed")
    if "SHA256SUMS.json" not in members:
        raise Rejected("manifest_missing", "Archive manifest required")
    # Prevent manifest references from escaping the private extracted copy before calling peer code.
    for name in json.loads(members["SHA256SUMS.json"]):
        safe_member(name)
        if name not in members:
            raise Rejected("member_missing", name)
    runtime = ROOT / "research/runtime/ws_import"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temporary:
        folder = Path(temporary)
        for name, body in members.items():
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        check = verifier()["verify"](folder, expected_raw_sha256=anchors["raw_sha256"])
        # Random private copy paths are operational metadata, excluded from deterministic results.
        check.pop("package_root", None)
        if check["status"] != "pass" or not check["input_hashes_unchanged"]:
            raise Rejected("capture_verification_failed", json.dumps(check["errors"]))
        actual = {
            p.relative_to(folder).as_posix(): sha(p.read_bytes()) for p in folder.rglob("*") if p.is_file()
        }
        if actual != {name: sha(body) for name, body in members.items()}:
            raise Rejected("input_changed_during_verification", "Extracted input changed")
    if sha(path.read_bytes()) != sha(blob):
        raise Rejected("input_changed_during_verification", "Archive changed during import")
    return raw, {
        "zip_sha256": sha(blob),
        "raw_sha256": sha(raw),
        "verifier_source_sha256": VENDOR_SHA,
        "members": {name: sha(body) for name, body in sorted(members.items())},
        "readonly_check": check,
        "input_archive_unchanged": True,
        "unlisted_policy": "Peer reports unlisted files but ignores .pyc/__pycache__ in that list, without failing on extras. External ZIP anchor covers every byte and wrapper before/after checks every extracted member. No archive scripts executed.",
    }

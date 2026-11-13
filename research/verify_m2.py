"""M2 R2 controlled reproduction. Real-window execution is disabled in this revision."""

import gzip
import json
import sys
from pathlib import Path

from .data_audit import DEFAULT, digest, verify
from .eligibility import render
from .engine import Rejected
from .execution_audit import counts, read_log, run_logged
from .m2 import RESULTS, render_results, require_controlled_snapshot


def artifact_digest(path):
    body = path.read_bytes()
    if path.name.endswith(".gz"):
        payload = gzip.decompress(body)
        return {
            "encoding": "gzip",
            "uncompressed_sha256": digest(payload),
            "uncompressed_bytes": len(payload),
        }
    return digest(body)


def check_manifest(root, manifest):
    for path, expected in manifest.items():
        if artifact_digest(root / path) != expected:
            raise ValueError(f"M2 R2 checksum mismatch: {path}")


def verify_controlled():
    require_controlled_snapshot()
    verify(DEFAULT)
    for name, content in render_results().items():
        if (RESULTS / name).read_text(encoding="utf-8") != content:
            raise ValueError(f"M2 accounting result mismatch: {name}")
    review = Path(__file__).parent / "results/m2_review_r2"
    manifest = json.loads((review / "manifest.json").read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parent.parent
    check_manifest(root, manifest)
    for name, content in render().items():
        stored = (review / name).read_bytes()
        # gzip OS/header bytes differ across Python releases; compare the actual CSV.
        if name.endswith(".gz"):
            stored, content = gzip.decompress(stored), gzip.decompress(content)
        if stored != content:
            raise ValueError(f"M2 eligibility result mismatch: {name}")
    journal = read_log(root / "research/audit/m2_r2/executions.jsonl")
    observed = counts(journal)
    print("M2 R2 verified: synthetic ledgers and 49,674 liquidity-aware daily rows; no real-window rerun")
    print(f"Captured local log counts: {observed}; legacy real-run totals remain unknown")
    return {
        "review_manifest_sha256": digest((review / "manifest.json").read_bytes()),
        "accounting_manifest_sha256": digest((RESULTS / "manifest.json").read_bytes()),
        "observed_log_counts": observed,
        "real_window_runs_this_operation": 0,
    }


def main():
    try:
        run_logged("controlled-verify-r2", verify_controlled)
    except Rejected as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

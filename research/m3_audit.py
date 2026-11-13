"""M3 CLI-only local append evidence; not a global count or external attestation."""

import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .execution_audit import append, canonical, sha

ROOT = Path(__file__).resolve().parent.parent


def audited(action, callback):
    freeze = ROOT / "research/M3_R2_SOURCE_FREEZE_V2.json"
    spec = json.loads(freeze.read_text(encoding="utf-8"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    common = {
        "run_id": str(uuid.uuid4()),
        "operation": "m3-" + action,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "HEAD": head.stdout.strip() or None,
        "python": sys.version.split()[0],
        "argv": list(sys.argv),
        "freeze_sha256": sha(freeze.read_bytes()),
        "source_sha256": {path: sha((ROOT / path).read_bytes()) for path in spec["sha256"]},
        "scope": "local M3 CLI entries only; tests/programmatic calls not counted; not real-window evidence",
    }
    log = ROOT / "research/runtime/m3_r2.jsonl"
    append(log, {**common, "event": "start"})
    try:
        result = callback()
    except Exception as exc:
        result = {"error_type": type(exc).__name__, "code": getattr(exc, "code", None)}
        append(
            log,
            {
                **common,
                "event": "finish",
                "status": "failed",
                "finished_at_utc": datetime.now(UTC).isoformat(),
                "result": result,
                "result_sha256": sha(canonical(result)),
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
    return result

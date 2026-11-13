"""Local append logs: scoped evidence, not external attestation or a global anti-rerun guarantee."""

import fcntl
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_LOG = ROOT / "research/runtime/executions.jsonl"
SOURCES = ("engine.py", "eligibility.py", "m2.py", "verify_m2.py", "execution_audit.py")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read_log(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        claimed = entry.pop("entry_sha256")
        if (
            sha(canonical(entry)) != claimed
            or entry["seq"] != len(result)
            or entry["previous_sha256"] != (result[-1]["entry_sha256"] if result else "0" * 64)
        ):
            raise ValueError("Execution log sequence or hash mismatch")
        if entry["event"] == "finish" and sha(canonical(entry["result"])) != entry["result_sha256"]:
            raise ValueError("Execution result hash mismatch")
        entry["entry_sha256"] = claimed
        result.append(entry)
    return result


def append(path, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        previous = read_log(path)
        entry = {
            **event,
            "seq": len(previous),
            "previous_sha256": previous[-1]["entry_sha256"] if previous else "0" * 64,
        }
        entry["entry_sha256"] = sha(canonical(entry))
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        fcntl.flock(handle, fcntl.LOCK_UN)


def counts(entries):
    return {
        "attempts": sum(e["event"] == "start" for e in entries),
        "completed": sum(e["event"] == "finish" and e["status"] == "passed" for e in entries),
        "failed": sum(e["event"] == "finish" and e["status"] == "failed" for e in entries),
        "recorded_real_window_entry_requests": sum(
            e["event"] == "start" and e["operation"] == "m2-integrate-real-once" for e in entries
        ),
        "scope": "available local append log only; not a historical/global execution count",
    }


def run_logged(operation, callback, path=None):
    path = path or RUNTIME_LOG
    started = datetime.now(UTC).isoformat()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    common = {
        "run_id": str(uuid.uuid4()),
        "operation": operation,
        "started_at_utc": started,
        "HEAD": head.stdout.strip() if head.returncode == 0 else None,
        "python": sys.version.split()[0],
        "argv": list(sys.argv),
        "source_sha256": {name: sha((ROOT / "research" / name).read_bytes()) for name in SOURCES},
    }
    append(path, {**common, "event": "start"})
    try:
        result = callback()
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "code": getattr(exc, "code", None)}
        append(
            path,
            {
                **common,
                "event": "finish",
                "status": "failed",
                "finished_at_utc": datetime.now(UTC).isoformat(),
                "result_sha256": sha(canonical(result)),
                "result": result,
            },
        )
        raise
    append(
        path,
        {
            **common,
            "event": "finish",
            "status": "passed",
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "result_sha256": sha(canonical(result)),
            "result": result,
        },
    )
    return result

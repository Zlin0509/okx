"""Additional current-process private-input guard for the synthetic experiment CLI."""

import os
import sys
from pathlib import Path


def install(root):
    prefixes = [
        root / "research" / p for p in ("snapshots", "results", "audit", "ws_data", "ws_results", "ws_audit")
    ]
    prefixes += [root.parent / p for p in ("ws-input", "ws-peer", "ws-integration-input")]
    counts = {"blocked_controlled_inputs": 0}

    def guard(event, args):
        if (
            event in {"open", "os.listdir", "os.scandir"}
            and args
            and isinstance(args[0], (str, bytes, os.PathLike))
        ):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(path == p or p in path.parents for p in prefixes):
                counts["blocked_controlled_inputs"] += 1
                raise PermissionError("synthetic_experiment_controlled_inputs_forbidden")

    sys.addaudithook(guard)
    return counts

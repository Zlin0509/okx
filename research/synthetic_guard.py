"""Regression guard against original snapshots and network; not a process security sandbox."""

import json
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def install():
    forbidden = (ROOT / "research/snapshots").resolve()
    report = {"blocked_snapshot_accesses": 0, "blocked_network_connects": 0}

    def guard(event, args):
        if event == "socket.connect":
            report["blocked_network_connects"] += 1
            raise PermissionError("synthetic_only_network_forbidden")
        if event in {"open", "os.listdir", "os.scandir"} and args:
            value = args[0]
            if isinstance(value, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(value)).absolute()
                # Resolve aliases before comparing; no snapshot content is opened.
                path = path.resolve()
                if path == forbidden or forbidden in path.parents:
                    report["blocked_snapshot_accesses"] += 1
                    raise PermissionError("synthetic_only_snapshot_forbidden")

    sys.addaudithook(guard)
    return report


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"pytest", "m3"}:
        raise SystemExit("Use: python -m research.synthetic_guard {pytest|m3} [args]")
    mode = sys.argv[1]
    report = install()
    if mode == "pytest":
        import pytest

        result = pytest.main(sys.argv[2:])
        print(
            json.dumps({"synthetic_guard": report, "scope": "this Python process and its threads only"}),
            file=sys.stderr,
        )
        raise SystemExit(3 if any(report.values()) else result)
    sys.argv = ["research.m3", *sys.argv[2:]]
    runpy.run_module("research.m3", run_name="__main__")


if __name__ == "__main__":
    main()

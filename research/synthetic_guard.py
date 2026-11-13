"""Regression guard against original snapshots and network; not a process security sandbox.

Scope: the current Python process and its threads only. Subprocesses, native
C extensions and code paths that do not emit the Python audit events below are
not covered. Installing this hook is not OS-level isolation and cannot prove
the absence of out-of-process activity.
"""

import json
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GUARD_SCOPE = (
    "current Python process and its threads only; subprocesses, native C calls "
    "and OS-level isolation are not covered"
)

# CPython raises these audit events at the Python entry point, before the
# corresponding resolver or connect syscall runs. Blocking them keeps the
# synthetic-only reproduction from reaching real name resolution or a socket.
NETWORK_EVENTS = {
    "socket.connect": ("blocked_network_connects", "synthetic_only_network_forbidden"),
    "socket.getaddrinfo": ("blocked_network_resolutions", "synthetic_only_dns_forbidden"),
    "socket.gethostbyname": ("blocked_network_resolutions", "synthetic_only_dns_forbidden"),
    "socket.gethostbyname_ex": ("blocked_network_resolutions", "synthetic_only_dns_forbidden"),
    "socket.gethostbyaddr": ("blocked_network_resolutions", "synthetic_only_dns_forbidden"),
    "socket.getnameinfo": ("blocked_network_resolutions", "synthetic_only_dns_forbidden"),
}


def install():
    forbidden = (ROOT / "research/snapshots").resolve()
    report = {
        "blocked_snapshot_accesses": 0,
        "blocked_network_connects": 0,
        "blocked_network_resolutions": 0,
    }

    def guard(event, args):
        network = NETWORK_EVENTS.get(event)
        if network is not None:
            counter, message = network
            report[counter] += 1
            raise PermissionError(message)
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
            json.dumps({"synthetic_guard": report, "scope": GUARD_SCOPE}),
            file=sys.stderr,
        )
        raise SystemExit(3 if any(report.values()) else result)
    sys.argv = ["research.m3", *sys.argv[2:]]
    runpy.run_module("research.m3", run_name="__main__")


if __name__ == "__main__":
    main()

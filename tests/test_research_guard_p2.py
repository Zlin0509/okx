"""Guard P2 evidence: CLI counts and DNS/connect refusal (synthetic/mock only)."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZERO_COUNTS = {
    "blocked_snapshot_accesses": 0,
    "blocked_network_connects": 0,
    "blocked_network_resolutions": 0,
}


def run_child(code):
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_guard_scope_is_explicitly_process_local():
    import research.synthetic_guard as guard

    assert "current Python process" in guard.GUARD_SCOPE
    assert "subprocesses" in guard.GUARD_SCOPE
    assert "OS-level isolation are not covered" in guard.GUARD_SCOPE


def test_synthetic_cli_reports_actual_guard_counts():
    result = subprocess.run(
        [sys.executable, "-m", "research.m3", "verify"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["cases"] == 12
    assert payload["real_window_reads"] == 0
    assert payload["synthetic_guard"] == ZERO_COUNTS
    assert "subprocesses" in payload["guard_scope"]


def test_dns_mock_is_refused_before_resolver_body_runs():
    child = r"""
import json
import socket
import sys
from unittest import mock

import research.synthetic_guard as guard

report = guard.install()
reached = []


def fake_getaddrinfo(host, port, *args, **kwargs):
    # CPython emits the audit event at resolver entry; the resolver body below
    # stands in for the real libc call and must never be reached.
    sys.audit("socket.getaddrinfo", host, port, *args)
    reached.append((host, port))
    raise AssertionError("resolver body reached")


with mock.patch.object(socket, "getaddrinfo", fake_getaddrinfo):
    try:
        socket.getaddrinfo("example.invalid", 80)
    except PermissionError as error:
        assert str(error) == "synthetic_only_dns_forbidden", str(error)
    else:
        raise AssertionError("guard did not block getaddrinfo")

assert reached == [], reached
print(json.dumps({"report": report}))
"""
    payload = run_child(child)
    assert payload["report"]["blocked_network_resolutions"] == 1
    assert payload["report"]["blocked_network_connects"] == 0


def test_all_declared_dns_entrypoints_are_blocked():
    child = r"""
import json
import sys

import research.synthetic_guard as guard

report = guard.install()
events = [
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.gethostbyname_ex",
    "socket.gethostbyaddr",
    "socket.getnameinfo",
]
for event in events:
    try:
        sys.audit(event, "example.invalid", 80)
    except PermissionError as error:
        assert str(error) == "synthetic_only_dns_forbidden", str(error)
    else:
        raise AssertionError(event + " was not blocked")
print(json.dumps({"report": report, "events": events}))
"""
    payload = run_child(child)
    assert payload["report"]["blocked_network_resolutions"] == len(payload["events"])
    assert payload["report"]["blocked_network_connects"] == 0


def test_real_numeric_dns_entrypoints_blocked_before_resolution():
    child = r"""
import json
import socket

import research.synthetic_guard as guard

report = guard.install()


def expect_block(call):
    try:
        call()
    except PermissionError as error:
        assert str(error) == "synthetic_only_dns_forbidden", str(error)
    else:
        raise AssertionError("guard did not block a real DNS entry point")


# Numeric hosts and numeric-result flags make every fallback network-free, so
# this exercises CPython's real audit event without any resolver traffic.
expect_block(
    lambda: socket.getaddrinfo(
        "127.0.0.1", 80, family=socket.AF_INET, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
    )
)
expect_block(lambda: socket.gethostbyname("127.0.0.1"))
expect_block(
    lambda: socket.getnameinfo(("127.0.0.1", 80), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)
)
print(json.dumps({"report": report}))
"""
    payload = run_child(child)
    assert payload["report"]["blocked_network_resolutions"] == 3
    assert payload["report"]["blocked_network_connects"] == 0


def test_connect_audit_is_refused_before_socket_call():
    child = r"""
import json
import socket
import sys

import research.synthetic_guard as guard

report = guard.install()
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    # CPython emits socket.connect immediately before the connect(2) syscall.
    # The guard must raise here, so no connection is attempted.
    sys.audit("socket.connect", sock, ("127.0.0.1", 9))
except PermissionError as error:
    assert str(error) == "synthetic_only_network_forbidden", str(error)
else:
    raise AssertionError("guard did not block connect")
finally:
    sock.close()
print(json.dumps({"report": report}))
"""
    payload = run_child(child)
    assert payload["report"]["blocked_network_connects"] == 1
    assert payload["report"]["blocked_network_resolutions"] == 0


def test_cli_reports_guard_counts_when_a_trip_aborts_the_run():
    child = r"""
import contextlib
import io
import json
import sys

import research.m3 as m3


def blocked(action):
    raise PermissionError("synthetic_only_dns_forbidden")


m3.perform = blocked
sys.argv = ["research.m3", "verify"]
stderr = io.StringIO()
code = None
with contextlib.redirect_stderr(stderr):
    try:
        m3.main()
    except SystemExit as error:
        code = error.code
assert code == 3, code
payload = json.loads(stderr.getvalue().strip().splitlines()[-1])
assert payload["guard_error"] == "synthetic_only_dns_forbidden"
assert payload["synthetic_guard"] == {
    "blocked_snapshot_accesses": 0,
    "blocked_network_connects": 0,
    "blocked_network_resolutions": 0,
}
print(json.dumps({"code": code, "payload": payload}))
"""
    payload = run_child(child)
    assert payload["code"] == 3
    assert payload["payload"]["synthetic_guard"]["blocked_network_resolutions"] == 0

"""Integrated CLI counters must record a real mock audit trip, before any resolver body."""

import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["research.m3", "research.statistics.runner"])
def test_integrated_CLI_mock_DNS_trip_reports_actual_counter_and_exit_three(module):
    code = r"""
import contextlib, importlib, io, json, sys
module = importlib.import_module(sys.argv[1])
ran = []
def mocked(action):
    sys.audit("socket.getaddrinfo", "synthetic.invalid", 80, 0, 0, 0)
    ran.append("resolver body must never run")
module.perform = mocked
sys.argv = ["synthetic-entry", "verify"]
stderr = io.StringIO()
with contextlib.redirect_stderr(stderr):
    try:
        module.main()
    except SystemExit as error:
        status = error.code
assert status == 3, status
payload = json.loads(stderr.getvalue().splitlines()[-1])
assert payload["synthetic_guard"]["blocked_network_resolutions"] == 1
assert payload["synthetic_guard"]["blocked_network_connects"] == 0
assert not ran
print(json.dumps({"exit_code": status, "payload": payload, "resolver_body_calls": len(ran)}, sort_keys=True))
"""
    p = subprocess.run([sys.executable, "-c", code, module], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    payload = json.loads(p.stdout)
    assert payload["exit_code"] == 3
    assert payload["resolver_body_calls"] == 0

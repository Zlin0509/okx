import importlib.util
import json
import os
import subprocess
import sys

import pytest
from conftest import FakeOKX
from fastapi.testclient import TestClient

from okx_workbench.app import create_app
from okx_workbench.config import Settings

spec = importlib.util.spec_from_file_location("desktop_server", "packaging/macos/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def write_demo(root, mode=0o600):
    path = root / "demo-credentials.json"
    path.write_text(
        json.dumps(
            {"api_key": "fake-api-key", "secret_key": "fake-secret-key", "passphrase": "fake-passphrase"}
        )
    )
    path.chmod(mode)
    return path


def test_offline_profile_never_reads_demo_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("OKX_API_KEY", "discarded-inherited-marker")
    monkeypatch.setenv("OKX_MODE", "live")
    monkeypatch.setenv("DATABASE_PATH", "outside.sqlite3")
    monkeypatch.setattr(server, "load_demo_credentials", lambda *args: pytest.fail("credentials read"))
    server.configure_environment(tmp_path, "offline")
    config = Settings.from_env()
    assert config.source == "offline" and config.mode == "demo" and not config.credentials_ready
    assert config.database == str(tmp_path / "workbench.sqlite3")
    assert config.max_order == config.max_position == 25


@pytest.mark.parametrize("kind", ["absent", "world_readable", "symlink", "invalid", "empty"])
def test_online_configuration_fails_closed(tmp_path, kind):
    if kind != "absent":
        path = write_demo(tmp_path, 0o644 if kind == "world_readable" else 0o600)
        if kind == "symlink":
            target = tmp_path / "actual.json"
            path.rename(target)
            path.symlink_to(target)
        elif kind == "invalid":
            path.write_text("{")
        elif kind == "empty":
            path.write_text("{}")
    with pytest.raises(ValueError):
        server.load_demo_credentials(tmp_path)


def test_native_demo_configuration_only_reaches_backend_and_source_switch_pauses(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    write_demo(tmp_path)
    server.configure_environment(tmp_path, "offline")
    offline = create_app(Settings.from_env())
    with TestClient(offline) as client:
        generated = client.post("/api/strategies/generate", json={}).json()
        epoch = client.get("/api/state").json()["control_epoch"]
        assert (
            client.post(
                f"/api/strategies/{generated['id']}/start",
                json={"confirmation": "START_OFFLINE", "control_epoch": epoch},
            ).status_code
            == 200
        )
    config = server.configure_environment(tmp_path, "okx")
    assert config.credentials_ready and config.mode == "demo"
    assert not Settings.from_env().credentials_ready
    assert "OKX_API_KEY" not in os.environ and "OKX_SECRET_KEY" not in os.environ
    exchange = FakeOKX()
    with TestClient(create_app(config, exchange)) as client:
        state = client.get("/api/state")
        assert state.json()["source"] == "okx" and not state.json()["live_supported"]
        assert all(not s["active"] for s in state.json()["strategies"])
        for secret in ("fake-api-key", "fake-secret-key", "fake-passphrase"):
            assert secret not in state.text and secret not in client.get("/").text
        assert client.get("/api/desktop/config").status_code == 404
        old_scope = client.app.state.engine.scope
    other = FakeOKX()
    other.uid = "99999999"
    with TestClient(create_app(config, other)) as client:
        assert client.get("/api/state").json()["startup_error"]
        assert all(not s["active"] for s in client.get("/api/state").json()["strategies"])
    assert old_scope and not exchange.submissions and not other.submissions


def test_desktop_never_accepts_live_source(tmp_path):
    with pytest.raises(ValueError, match="仅支持"):
        server.configure_environment(tmp_path, "live")


@pytest.mark.parametrize(
    "permissions,allowed", [("read_only,trade", True), ("read_only,trade,withdraw", False), ("", False)]
)
def test_packaged_start_checks_demo_permissions(setup_engine, permissions, allowed):
    engine, store, exchange, sid = setup_engine
    store.stop()
    exchange.request = lambda *args, **kwargs: [{"perm": permissions, "posMode": "net_mode"}]
    server.guard_demo_start(engine)
    if allowed:
        engine.start(sid)
        assert store.strategies()[0]["active"]
    else:
        from okx_workbench.market import TradingError

        with pytest.raises(TradingError, match="保持暂停"):
            engine.start(sid)
        assert not store.strategies()[0]["active"]
    assert not exchange.submissions


def test_controlled_desktop_runtime_signal_produces_exactly_one_order():
    completed = subprocess.run(
        [sys.executable, "packaging/macos/server.py", "--self-check-offline"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {
        "test": "controlled_offline_fresh_signal",
        "simulated_clock": True,
        "real_okx": False,
        "first_action": "submitted",
        "second_action": "duplicate",
        "orders": 1,
        "filled": result["filled"],
        "strategy_paused": True,
    }
    assert float(result["filled"]) > 0

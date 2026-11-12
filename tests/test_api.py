from dataclasses import replace

from fastapi.testclient import TestClient

from okx_workbench.app import create_app
from okx_workbench.config import Settings


def test_full_offline_workflow_and_restart(tmp_path):
    settings = Settings(database=str(tmp_path / "test.sqlite3"))
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/health").json() == {"status": "ok", "mode": "demo", "source": "offline"}
        generated = client.post("/api/strategies/generate", json={}).json()
        sid = generated["id"]
        assert not client.get("/api/state").json()["strategies"][0]["active"]
        report = client.post("/api/backtests", json=generated["spec"]).json()
        csv = client.get(f"/api/backtests/{report['id']}/export.csv")
        assert csv.status_code == 200 and "ts,equity" in csv.text
        assert len(csv.text.splitlines()) == len(report["curve"]) + 1
        assert (
            client.post(f"/api/strategies/{sid}/start", json={"confirmation": "START_LIVE"}).status_code
            == 409
        )
        assert (
            client.post(f"/api/strategies/{sid}/start", json={"confirmation": "START_OFFLINE"}).status_code
            == 200
        )
        first = client.post(f"/api/strategies/{sid}/tick", json={})
        assert first.status_code == 200
        assert client.post(f"/api/strategies/{sid}/tick", json={}).status_code == 200
        account = client.get("/api/account").json()
        assert account["equity"] == account["initial_capital"] + account["net_pnl"]
        assert client.post("/api/stop", json={}).json()["stopped"]
        assert client.post(f"/api/strategies/{sid}/tick", json={}).status_code == 409
        client.post(f"/api/strategies/{sid}/start", json={"confirmation": "START_OFFLINE"})
    with TestClient(create_app(settings)) as restarted:
        assert not restarted.get("/api/state").json()["strategies"][0]["active"]


def test_input_security_and_no_secrets(tmp_path):
    settings = replace(
        Settings(),
        database=str(tmp_path / "test.sqlite3"),
        api_key="private-api-key",
        secret="private-secret",
        passphrase="private-pass",
    )
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/strategies/generate", json={"allocation": 1}).status_code == 422
        assert client.get("/api/market?symbol=BTC-USD-SWAP").status_code == 422
        assert (
            client.post("/api/stop", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        )
        assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 400
        assert (
            client.post("/api/stop", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
        )
        state = client.get("/api/state")
        assert settings.api_key not in state.text and settings.secret not in state.text
        assert settings.passphrase not in state.text
        assert "frame-ancestors 'none'" in state.headers["content-security-policy"]

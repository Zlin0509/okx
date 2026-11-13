import fcntl
from argparse import Namespace
from decimal import Decimal

import pytest
from conftest import FakeOKX

from okx_workbench.config import Settings
from okx_workbench.execution import Engine
from okx_workbench.store import Store
from scripts import demo_smoke


class SmokeOKX(FakeOKX):
    permissions = "read_only,trade"

    def request(self, method, path, *args, **kwargs):
        if path == "/api/v5/account/config":
            return [{"perm": self.permissions, "posMode": "net_mode"}]
        if path in {"/api/v5/trade/fills-history", "/api/v5/account/bills"}:
            return []
        pytest.fail("Unexpected mock operation")


@pytest.mark.parametrize("existing", [False, True])
def test_demo_smoke_preflight_never_places_and_preserves_existing(tmp_path, monkeypatch, existing):
    exchange = SmokeOKX()
    exchange.position = "1" if existing else "0"
    monkeypatch.setattr(
        demo_smoke, "credentials", lambda path: {"api_key": "key", "secret": "secret", "passphrase": "pass"}
    )
    captured = []

    def create(settings):
        captured.append(settings)
        exchange.settings = settings
        return exchange

    monkeypatch.setattr(demo_smoke, "OKX", create)
    result = demo_smoke.run(
        Namespace(credentials_file="unused", database=str(tmp_path / "test.sqlite3"), execute_demo=False)
    )
    assert not exchange.submissions
    assert captured[0].mode == "demo" and captured[0].max_order == Decimal("25")
    assert result["status"] == ("blocked" if existing else "preflight_only")
    assert result["strategy_paused"]
    assert result["permissions"] == {
        "read": True,
        "trade": True,
        "withdraw": False,
        "position_mode": "net_mode",
    }
    assert exchange.position == ("1" if existing else "0")
    # Main application uses the same UID-bound ledger after smoke exits.
    application = Engine(captured[0], Store(captured[0].database), exchange)
    assert application.scope


def test_smoke_cleanup_refuses_unattributed_position(setup_engine):
    engine, _, exchange, sid = setup_engine
    exchange.position = "2"
    with pytest.raises(demo_smoke.TradingError, match="不符"):
        demo_smoke.cleanup(engine, sid, "existing-entry", Decimal("0.16"), {"symbol": "BTC-USDT-SWAP"})
    assert not exchange.submissions


def test_smoke_cleanup_persists_submit_marker_and_only_reduces_owned_position(setup_engine, monkeypatch):
    engine, store, exchange, sid = setup_engine
    exchange.position = "0.16"
    original = exchange.place

    def fill(body):
        result = original(body)
        exchange.position = "0"
        return result

    monkeypatch.setattr(exchange, "place", fill)
    monkeypatch.setattr(demo_smoke.time, "sleep", lambda _: None)
    summary = {"symbol": "BTC-USDT-SWAP"}
    demo_smoke.cleanup(engine, sid, "owned-entry", Decimal("0.16"), summary)
    assert summary["position_after_cleanup"] == "0" and summary["strategy_paused"]
    assert exchange.submissions[0]["reduceOnly"] is True
    order = store.orders(engine.scope)[0]
    assert order["submit_started"] is not None and order["state"] == "filled"
    assert store.audit_history(order["id"])[0]["to_state"] == "submitting"


@pytest.mark.parametrize("environment", [None, "data/custom.sqlite3"])
def test_smoke_default_database_matches_application_or_explicit_environment(monkeypatch, environment):
    if environment:
        monkeypatch.setenv("DATABASE_PATH", environment)
    else:
        monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setattr("sys.argv", ["demo_smoke", "--credentials-file", "unused"])
    captured = []
    monkeypatch.setattr(
        demo_smoke, "run", lambda args: captured.append(args.database) or {"status": "preflight_only"}
    )
    assert demo_smoke.main() == 0
    assert captured == [environment or Settings().database]


def test_smoke_rejects_running_main_application_before_reading_credentials(tmp_path, monkeypatch):
    path = tmp_path / "application.sqlite3"
    monkeypatch.setattr(demo_smoke, "credentials", lambda *args: pytest.fail("credentials read"))
    with path.with_suffix(".sqlite3.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = demo_smoke.run(Namespace(database=str(path), credentials_file="unused", execute_demo=True))
    assert result["status"] == "blocked" and "先停止服务" in result["error"]


@pytest.mark.parametrize(
    "permissions", ["", "read_only", "trade", "read_only,trade,withdraw", "read_only,trade,unknown"]
)
def test_smoke_permissions_fail_closed(permissions):
    exchange = SmokeOKX()
    exchange.permissions = permissions
    with pytest.raises(demo_smoke.TradingError, match="权限"):
        demo_smoke.check_permissions(exchange)


@pytest.mark.parametrize(
    "side,size,price,reduce_only,accepted",
    [
        ("buy", "0.01", "60000", False, True),
        ("buy", "0.02", "60000", False, False),
        ("buy", "0.01", "260000", False, False),
        ("sell", "0.01", "60000", True, True),
        ("sell", "0.01", "60000", False, False),
        ("sell", "0.02", "60000", True, False),
    ],
)
def test_smoke_final_guard_enforces_minimum_and_cap(setup_engine, side, size, price, reduce_only, accepted):
    engine, _, exchange, _ = setup_engine
    quantity, budget = demo_smoke.minimum_order_plan(
        exchange, engine.settings, "BTC-USDT-SWAP", exchange.instrument("BTC-USDT-SWAP")
    )
    assert quantity == Decimal("0.01") and budget < 25
    body = {
        "instId": "BTC-USDT-SWAP",
        "side": side,
        "sz": size,
        "px": price,
        "reduceOnly": reduce_only,
        "ordType": "ioc",
    }
    if accepted:
        exchange.place(body)
    else:
        with pytest.raises(demo_smoke.ExchangeRejected, match="未 POST"):
            exchange.place(body)
    assert len(exchange.submissions) == int(accepted)


def test_minimum_above_cap_stops_before_order_guard_or_post(setup_engine):
    engine, _, exchange, _ = setup_engine
    info = {**exchange.instrument("BTC-USDT-SWAP"), "minSz": "1"}
    with pytest.raises(demo_smoke.TradingError, match="超过 25"):
        demo_smoke.minimum_order_plan(exchange, engine.settings, "BTC-USDT-SWAP", info)
    assert not exchange.submissions


def test_missing_credentials_stops_without_api_or_sensitive_output(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_smoke, "OKX", lambda *args: pytest.fail("API client created"))
    with pytest.raises(demo_smoke.TradingError, match="凭据文件不存在"):
        demo_smoke.run(
            Namespace(
                credentials_file=str(tmp_path / "absent-demo.json"),
                database=str(tmp_path / "db.sqlite3"),
                execute_demo=True,
            )
        )


def test_masked_order_summary_excludes_full_ids():
    result = demo_smoke.summarize_order(
        {"clOrdId": "wb12345678901234", "ordId": "9876543210123456", "state": "filled"}
    )
    assert result["client_id_masked"] == "wb12…1234"
    assert result["exchange_id_masked"] == "9876…3456"


def test_full_smoke_minimum_signal_fill_account_sync_and_cleanup(tmp_path, monkeypatch):
    class FilledSmoke(SmokeOKX):
        def __init__(self):
            super().__init__()
            self.orders = {}

        def place(self, body):
            response = super().place(body)
            self.position = body["sz"] if body["side"] == "buy" else "0"
            self.orders[body["clOrdId"]] = {
                "state": "filled",
                "accFillSz": body["sz"],
                "clOrdId": body["clOrdId"],
                "ordId": "1234567890123",
                "avgPx": body["px"],
            }
            return response

        def order(self, symbol, client_id):
            return self.orders[client_id]

    exchange = FilledSmoke()

    def create(settings):
        exchange.settings = settings
        return exchange

    monkeypatch.setattr(demo_smoke, "OKX", create)
    monkeypatch.setattr(
        demo_smoke, "credentials", lambda *args: {"api_key": "fake", "secret": "fake", "passphrase": "fake"}
    )
    monkeypatch.setattr(demo_smoke.time, "sleep", lambda *args: None)
    result = demo_smoke.run(
        Namespace(credentials_file="unused", database=str(tmp_path / "db.sqlite3"), execute_demo=True)
    )
    assert result["status"] == "filled_and_position_confirmed" and result["cleanup"] == "complete"
    assert result["minimum_contracts"] == result["submitted_contracts"] == "0.01"
    assert result["position_before"] == result["position_after_cleanup"] == "0"
    assert result["position_after_entry"] == "0.01"
    assert result["account_after_entry"]["equity_usdt"] == result["application_account_after_entry"]["equity"]
    assert result["account_after_cleanup"]["pending_count"] == 0
    assert result["strategy_paused"] and len(exchange.submissions) == 2
    assert exchange.submissions[-1]["reduceOnly"] is True
    assert result["automatic_post_retries"] == 0 and not result["request_failures"]

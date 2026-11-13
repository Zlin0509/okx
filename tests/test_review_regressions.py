import csv
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
from conftest import FakeOKX
from fastapi.testclient import TestClient

from okx_workbench.app import create_app
from okx_workbench.config import BARS, Settings
from okx_workbench.coordination import account_scope
from okx_workbench.execution import Engine
from okx_workbench.market import Candle, TradingError
from okx_workbench.store import Store
from okx_workbench.strategy import StrategySpec, backtest, generate


def seed(engine, store, sid, oid, state="unknown", version=0):
    with store.connect() as db:
        db.execute(
            """INSERT INTO orders(id,scope,strategy_id,symbol,side,quantity,price,state,created,updated,intent_version)
                      VALUES(?,?,?,'BTC-USDT-SWAP','buy','0.01','60000',?,?,?,?)""",
            (oid, engine.scope, sid, state, time.time(), time.time(), version),
        )


def missing(*args):
    raise TradingError("查无订单", "51603")


def test_crash_before_submit_marker_is_safe_to_cancel_without_replay(setup_engine, monkeypatch):
    engine, store, exchange, sid = setup_engine
    original, count = store.locked, 0

    class Crash(BaseException):
        pass

    @contextmanager
    def crash_before_marker():
        nonlocal count
        count += 1
        if count == 2:
            raise Crash()
        with original() as db:
            yield db

    monkeypatch.setattr(store, "locked", crash_before_marker)
    with pytest.raises(Crash):
        engine.execute(sid)
    monkeypatch.setattr(store, "locked", original)
    intent = store.orders(engine.scope)[0]
    assert intent["state"] == "reserved" and intent["submit_started"] is None
    monkeypatch.setattr(exchange, "order", lambda *args: pytest.fail("unsent intent queried exchange"))
    engine.reconcile(recover_unsent=True)
    assert store.orders(engine.scope)[0]["state"] == "canceled_local"
    assert engine.execute(sid)["action"] == "duplicate"
    assert not exchange.submissions
    assert store.audit_history(intent["id"])[-1]["to_state"] == "canceled_local"


def test_crash_after_submit_marker_requires_audited_manual_recovery_and_never_replays(setup_engine):
    engine, store, exchange, sid = setup_engine

    class Crash(BaseException):
        pass

    exchange.failure = Crash()
    with pytest.raises(Crash):
        engine.execute(sid)
    order = store.orders(engine.scope)[0]
    assert order["state"] == "submitting" and order["submit_started"] is not None
    exchange.order = missing
    for _ in range(3):
        engine.reconcile()
    assert store.orders(engine.scope)[0]["not_found_count"] == 3
    before = len(exchange.submissions)
    engine.abandon(
        order["id"], "测试维护者", "已核对交易所历史订单及成交且账户空仓", "ABANDON_WITHOUT_RESEND"
    )
    assert store.orders(engine.scope)[0]["state"] == "abandoned"
    assert not store.strategies()[0]["active"]
    audit = store.audit_history(order["id"])[-1]
    assert audit["actor"] == "测试维护者" and json.loads(audit["evidence"])["target_position"] == "0"
    rotated = Engine(replace(engine.settings, api_key="rotated-key"), store, exchange)
    rotated.start(sid)
    assert rotated.execute(sid)["action"] == "duplicate"
    assert len(exchange.submissions) == before


@pytest.mark.parametrize(
    "guard", ["fewer_checks", "network_error", "found", "position", "pending", "wrong_confirmation"]
)
def test_manual_recovery_guards(setup_engine, guard):
    engine, store, exchange, sid = setup_engine
    seed(engine, store, sid, "stuck")
    exchange.order = missing
    for _ in range(2 if guard == "fewer_checks" else 3):
        engine.reconcile()
    if guard == "network_error":
        exchange.order = lambda *args: (_ for _ in ()).throw(TradingError("认证失败", "50113"))
    if guard == "found":
        exchange.order = lambda *args: {"state": "filled", "accFillSz": "0.01"}
    if guard == "position":
        exchange.position = "0.01"
    if guard == "pending":
        exchange.pending = [{"ordId": "other"}]
    with pytest.raises(TradingError):
        engine.abandon(
            "stuck",
            "维护者",
            "已人工核对历史成交与挂单并记录依据",
            "WRONG" if guard == "wrong_confirmation" else "ABANDON_WITHOUT_RESEND",
        )
    assert store.orders(engine.scope)[0]["state"] == "unknown"
    assert not exchange.submissions


def test_per_order_lookup_failure_isolated_and_not_hidden_by_100_row_window(setup_engine):
    engine, store, exchange, sid = setup_engine
    seed(engine, store, sid, "stuck", "reserved")  # Legacy: submit outcome is unknown.
    seed(engine, store, sid, "will-fill", "accepted")
    for i in range(105):
        seed(engine, store, sid, f"terminal-{i}", "filled")
    exchange.order = lambda symbol, oid: (
        missing() if oid == "stuck" else {"state": "filled", "accFillSz": "0.01"}
    )
    results = {r["id"]: r for r in engine.reconcile()}
    assert results["stuck"]["state"] == "unknown" and results["stuck"]["code"] == "51603"
    assert results["will-fill"]["state"] == "filled"
    assert len(store.unresolved(engine.scope)) == 1
    assert not exchange.submissions


def test_offline_orphan_reservation_recovers_without_changing_ledger(setup_engine):
    online, store, exchange, sid = setup_engine
    engine = Engine(replace(online.settings, source="offline"), store, exchange)
    seed(engine, store, sid, "orphan", "reserved")
    before = engine.paper_account()
    assert engine.reconcile(recover_unsent=True)[0]["state"] == "canceled_local"
    assert engine.paper_account() == before
    assert not exchange.submissions


def test_startup_reconciles_without_activating_or_posting(setup_engine):
    engine, store, exchange, sid = setup_engine
    seed(engine, store, sid, "accepted", "accepted")
    with TestClient(create_app(engine.settings, exchange)) as client:
        state = client.get("/api/state").json()
        assert state["orders"][0]["state"] == "filled"
        assert not state["strategies"][0]["active"]
        assert client.get("/api/orders/accepted/audit").json()["audit"]
    assert not exchange.submissions


def test_pause_invalidates_slow_start_and_no_later_tick_can_submit(setup_engine):
    engine, store, exchange, sid = setup_engine
    entered, release = threading.Event(), threading.Event()
    original = exchange.account

    def delayed():
        entered.set()
        assert release.wait(3)
        return original()

    exchange.account = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(engine.start, sid, store.control_epoch())
        assert entered.wait(2)
        store.stop()
        assert not store.strategies()[0]["active"]
        release.set()
        with pytest.raises(TradingError, match="作废"):
            pending.result(timeout=3)
    engine.tick()
    with pytest.raises(TradingError, match="未启动"):
        engine.execute(sid)
    assert not store.strategies()[0]["active"] and not exchange.submissions


def test_queued_start_with_old_control_epoch_rejected(setup_engine):
    engine, store, exchange, sid = setup_engine
    old_epoch = store.control_epoch()
    store.stop()
    with pytest.raises(TradingError, match="作废"):
        engine.start(sid, old_epoch)
    assert not exchange.submissions


def test_multiple_databases_and_api_keys_same_uid_only_one_can_execute(tmp_path):
    exchange = FakeOKX()
    engines = []
    for i in range(2):
        settings = Settings(source="okx", api_key=f"key-{i}", database=str(tmp_path / f"db-{i}.sqlite3"))
        store = Store(settings.database)
        sid = store.add_strategy(
            {
                "spec": StrategySpec().model_dump(),
                "validation": {},
                "scope": account_scope("demo", exchange.uid),
            }
        )
        engines.append((Engine(settings, store, exchange), sid))

    def run(item):
        engine, sid = item
        try:
            engine.start(sid)
            return engine.execute(sid)["action"]
        except TradingError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, engines))
    assert results.count("submitted") == 1
    assert sum("绑定另一数据库" in r for r in results) == 1
    assert len(exchange.submissions) == 1


def test_same_database_key_rotation_keeps_account_identity_and_dedup(setup_engine):
    engine, store, exchange, sid = setup_engine
    engine.execute(sid)
    rotated = Engine(replace(engine.settings, api_key="different-key"), store, exchange)
    assert rotated.scope == engine.scope
    rotated.start(sid)
    assert rotated.execute(sid)["action"] == "duplicate"
    assert len(exchange.submissions) == 1


def test_unverified_other_legacy_key_intents_cannot_be_hidden_by_rotation(setup_engine):
    engine, store, exchange, sid = setup_engine
    seed(engine, store, sid, "legacy-foreign")
    with store.connect() as db:
        db.execute("UPDATE orders SET scope='demo:okx:unverifiedoldkey' WHERE id='legacy-foreign'")
    rotated = Engine(replace(engine.settings, api_key="rotated-again"), store, exchange)
    with pytest.raises(TradingError, match="其他旧版 Key"):
        rotated.start(sid)
    assert not exchange.submissions


def test_manual_recovery_api_audit_and_blocker_visibility(setup_engine):
    engine, store, exchange, sid = setup_engine
    seed(engine, store, sid, "old-unknown")
    exchange.order = missing
    with TestClient(create_app(engine.settings, exchange)) as client:
        state = client.get("/api/state").json()
        assert state["blockers"][0]["id"] == "old-unknown"
        for _ in range(2):
            assert client.post("/api/orders/reconcile", json={}).status_code == 200
        body = {
            "operator": "本机测试员",
            "reason": "已人工核对订单历史及成交，空仓无挂单",
            "confirmation": "ABANDON_WITHOUT_RESEND",
        }
        assert client.post("/api/orders/old-unknown/abandon", json=body).json()["resubmitted"] is False
        history = client.get("/api/orders/old-unknown/audit").json()["audit"]
        assert history[-1]["actor"] == "本机测试员"
        assert not client.get("/api/state").json()["blockers"]
        assert not client.get("/api/state").json()["strategies"][0]["active"]
    assert not exchange.submissions


def test_legacy_scope_migration_preserves_audit_and_blocks_old_signals(setup_engine):
    engine, store, exchange, sid = setup_engine
    engine.execute(sid)
    with store.connect() as db:
        db.execute("UPDATE orders SET scope=?", (engine.legacy_scope,))
    migrated = Engine(engine.settings, store, exchange)
    assert migrated.scope == engine.scope
    assert store.legacy_cutoff(migrated.scope) > 0
    assert migrated.execute(sid)["action"] == "legacy_signal_blocked"
    assert len(exchange.submissions) == 1


@pytest.mark.parametrize(
    "bar,age,submitted",
    [
        (b, a, ok)
        for b in ("1H", "4H", "1D")
        for a, ok in [(0, True), (300, True), (301, False), (23 * 3600, False), (-1, False)]
    ],
)
def test_five_minute_signal_window_all_timeframes(setup_engine, monkeypatch, bar, age, submitted):
    engine, store, exchange, sid = setup_engine
    step = BARS[bar]
    end = int(time.time() * 1000) // step * step
    candles = [Candle(end - (200 - i) * step, 60000 + i, 60020 + i, 59990 + i, 60001 + i) for i in range(200)]
    exchange.candles = lambda *args: candles
    with store.connect() as db:
        db.execute(
            "UPDATE strategies SET spec=? WHERE id=?", (json.dumps(StrategySpec(bar=bar).model_dump()), sid)
        )
    monkeypatch.setattr(engine, "now_ms", lambda: end + age * 1000)
    result = engine.execute(sid)
    assert result["signal"]["age_seconds"] == age
    assert bool(exchange.submissions) == submitted
    if not submitted:
        assert result["action"] == "expired"


def test_backtest_and_generation_use_execution_limits_and_default_slippage(setup_engine):
    engine, _, exchange, sid = setup_engine
    engine.execute(sid)
    body = exchange.submissions[0]
    actual = Decimal(body["sz"]) * Decimal("0.01") * Decimal(body["px"])
    report = backtest(exchange.candles("BTC-USDT-SWAP", "1H"), StrategySpec(), limits=engine.settings)
    theoretical = report["trades"][0]["entry"] * report["trades"][0]["quantity"]
    assert theoretical == pytest.approx(100)
    assert 0 <= Decimal(str(theoretical)) - actual < Decimal("0.01") * Decimal("0.01") * Decimal(body["px"])
    assert report["risk"]["slippage_bps"] == 20
    generated = generate(
        exchange.candles("BTC-USDT-SWAP", "1H"),
        StrategySpec(),
        replace(engine.settings, max_order=Decimal("37")),
    )
    for partition in ("training", "validation"):
        assert generated[partition]["risk"]["initial_effective_budget_usdt"] == 37
        assert all(t["entry"] * t["quantity"] <= 37.000001 for t in generated[partition]["trades"])


def test_backtest_csv_preserves_parameter_snapshot(tmp_path):
    settings = Settings(database=str(tmp_path / "api.sqlite3"))
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/backtests", json={"symbol": "BTC-USDT-SWAP", "fast": 8, "slow": 24}).json()
        client.post("/api/backtests", json={"symbol": "ETH-USDT-SWAP", "fast": 20, "slow": 80})
        rows = list(csv.DictReader(io.StringIO(client.get(f"/api/backtests/{first['id']}/export.csv").text)))
        assert rows[0]["symbol"] == "BTC-USDT-SWAP" and rows[0]["fast"] == "8" and rows[0]["slow"] == "24"
        assert rows[0]["slippage_bps"] == "20.0" and rows[0]["generated_at_ms"]

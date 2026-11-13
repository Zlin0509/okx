import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest
from conftest import FakeOKX

from okx_workbench.config import BARS, Settings
from okx_workbench.coordination import account_scope, bind_account
from okx_workbench.execution import Engine
from okx_workbench.market import OKX, TradingError
from okx_workbench.store import Store
from okx_workbench.strategy import StrategySpec


def legacy_database(tmp_path, state="filled"):
    settings = Settings(source="okx", api_key="new-key", database=str(tmp_path / "old.sqlite3"))
    store = Store(settings.database)
    old = "demo:okx:revoked-key-fingerprint"
    sid = store.add_strategy({"spec": StrategySpec().model_dump(), "scope": old})
    now = time.time()
    with store.connect() as db:
        db.execute(
            """INSERT INTO orders(id,scope,strategy_id,symbol,side,quantity,price,state,exchange_id,
                      created,updated,filled) VALUES('old-intent',?,?,'BTC-USDT-SWAP','buy','0.16',
                      '60000',?,'12345',?,?,'0.16')""",
            (old, sid, state, now, now),
        )
        store.audit(db, "old-intent", "accepted", state, "历史记录")
    exchange = FakeOKX()
    receipt = {
        "clOrdId": "old-intent",
        "instId": "BTC-USDT-SWAP",
        "side": "buy",
        "sz": "0.16",
        "ordId": "12345",
        "state": state,
        "accFillSz": "0.16",
    }
    exchange.order = lambda *args: receipt
    return settings, store, exchange, sid, old, receipt


@pytest.mark.parametrize("state", ["filled", "canceled", "mmp_canceled", "live"])
def test_revoked_legacy_key_migrates_using_verified_account_order(tmp_path, state):
    settings, store, exchange, sid, old, _ = legacy_database(tmp_path, state)
    engine = Engine(settings, store, exchange)
    scope = engine.scope
    assert scope == account_scope("demo", exchange.uid)
    row = store.orders(scope)[0]
    assert row["id"] == "old-intent" and row["state"] == state
    assert row["exchange_id"] == "12345" and row["filled"] == "0.16"
    assert not store.orders(old)
    assert store.strategies()[0]["report"]["scope"] == scope
    history = store.audit_history(row["id"])
    assert len(history) == 2 and history[0]["reason"] == "历史记录"
    evidence = json.loads(history[-1]["evidence"])
    assert evidence["ownership"] == "order_lookup_authenticated_account"
    assert evidence["account_scope"] == scope and evidence["clOrdId"] == row["id"]
    assert store.legacy_cutoff(scope) == row["created"]
    if state in {"filled", "canceled", "mmp_canceled"}:
        engine.start(sid)
        assert engine.execute(sid)["action"] == "legacy_signal_blocked"
    assert not exchange.submissions


@pytest.mark.parametrize(
    "field,value",
    [
        ("clOrdId", "someone-else"),
        ("instId", "ETH-USDT-SWAP"),
        ("side", "sell"),
        ("sz", "0.17"),
        ("ordId", "wrong-id"),
        ("state", "unknown"),
        ("state", "canceled"),
    ],
)
def test_migration_identity_mismatch_rolls_back_record_and_new_binding(tmp_path, field, value):
    settings, store, exchange, _, old, receipt = legacy_database(tmp_path)
    receipt[field] = value
    engine = Engine(settings, store, exchange)
    with pytest.raises(TradingError, match="拒绝迁移"):
        _ = engine.scope
    assert store.orders(old)[0]["state"] == "filled"
    assert len(store.audit_history("old-intent")) == 1
    with sqlite3.connect(settings.account_state_dir + "/accounts.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0
    # A handled failure must not leave a phantom claim on that UID.
    other = Engine(
        replace(settings, database=str(tmp_path / "other.sqlite3")),
        Store(str(tmp_path / "other.sqlite3")),
        exchange,
    )
    assert other.scope == account_scope("demo", exchange.uid)


def test_migration_missing_account_evidence_is_not_inferred_from_terminal_state(tmp_path):
    settings, store, exchange, _, old, _ = legacy_database(tmp_path)

    def missing(*args):
        raise TradingError("not found", "51603")

    exchange.order = missing
    with pytest.raises(TradingError, match="账户归属无法核实"):
        _ = Engine(settings, store, exchange).scope
    assert store.orders(old) and len(store.audit_history("old-intent")) == 1
    # Restoring the original key identity proves ownership even if remote retention expired.
    engine = Engine(settings, store, exchange)
    with store.connect() as db:
        db.execute("UPDATE orders SET scope=?", (engine.legacy_scope,))
    assert engine.scope == account_scope("demo", exchange.uid)
    assert len(store.audit_history("old-intent")) == 2


def test_migration_partial_work_and_binding_are_rolled_back_on_later_record_failure(tmp_path):
    settings, store, exchange, sid, old, _ = legacy_database(tmp_path)
    engine = Engine(settings, store, exchange)
    with store.connect() as db:
        # The authenticated current-key group sorts first and is updated before the
        # foreign-key group fails: the transaction must restore BOTH groups.
        db.execute(
            "INSERT INTO orders SELECT 'current-key',?,strategy_id,symbol,side,quantity,price,state,"
            "exchange_id,error,created,updated,filled,intent_version,submit_started,not_found_count,"
            "last_error_code FROM orders WHERE id='old-intent'",
            (engine.legacy_scope,),
        )
    exchange.order = lambda *args: {}
    with pytest.raises(TradingError):
        _ = engine.scope
    assert store.orders(old) and store.orders(engine.legacy_scope)
    assert len(store.audit_history("current-key")) == 0
    assert len(store.audit_history("old-intent")) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM scope_migrations").fetchone()[0] == 0
    assert store.strategies()[0]["id"] == sid


def test_failed_migration_keeps_preexisting_binding_and_crash_keeps_new_claim(tmp_path):
    directory = tmp_path / "coordination"
    scope = account_scope("demo", "12345")
    database = tmp_path / "original.sqlite3"
    bind_account(directory, scope, database)

    def fail():
        raise TradingError("migration failed")

    with pytest.raises(TradingError):
        bind_account(directory, scope, database, fail)
    with pytest.raises(TradingError, match="绑定另一数据库"):
        bind_account(directory, scope, tmp_path / "another.sqlite3")

    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    other_scope = account_scope("demo", "67890")
    with pytest.raises(Crash):
        bind_account(directory, other_scope, tmp_path / "crash.sqlite3", crash)
    with pytest.raises(TradingError, match="绑定另一数据库"):
        bind_account(directory, other_scope, tmp_path / "new.sqlite3")


def test_previous_build_abandoned_order_reenters_reconciliation(setup_engine):
    engine, store, exchange, sid = setup_engine
    engine.execute(sid)
    order = store.orders(engine.scope)[0]
    with store.connect() as db:
        db.execute("UPDATE orders SET state='abandoned' WHERE id=?", (order["id"],))
        store.audit(db, order["id"], "unknown", "abandoned", "旧版误解除", actor="old-operator")
    restarted = Engine(engine.settings, store, exchange)
    assert store.unresolved(restarted.scope)[0]["state"] == "unknown"
    store.stop()
    restarted.tick()
    assert store.orders(restarted.scope)[0]["state"] == "filled"
    assert any(a["actor"] == "old-operator" for a in store.audit_history(order["id"]))
    assert not store.strategies()[0]["active"] and len(exchange.submissions) == 1


@pytest.mark.parametrize("stage", ["account", "reserve_commit", "marker_commit", "pre_post"])
@pytest.mark.parametrize("bar", ["1H", "1m"])
def test_original_deadline_rechecked_after_slow_preflight_and_each_transaction(
    setup_engine, monkeypatch, stage, bar
):
    engine, store, exchange, sid = setup_engine
    candles = exchange.candles("BTC-USDT-SWAP", "1H")
    end = candles[-1].ts + 3600000
    if bar == "1m":
        candles = [replace(c, ts=end - (len(candles) - i) * BARS[bar]) for i, c in enumerate(candles)]
        monkeypatch.setattr(exchange, "candles", lambda *args: candles)
        with store.connect() as db:
            db.execute(
                "UPDATE strategies SET spec=? WHERE id=?",
                (json.dumps(StrategySpec(bar=bar).model_dump()), sid),
            )
    now = end + 299000
    monkeypatch.setattr(engine, "now_ms", lambda: now)
    original_account, original_locked = exchange.account, store.locked

    def delayed_account():
        nonlocal now
        result = original_account()
        now += 2500
        return result

    count = 0

    @contextmanager
    def delayed_transaction():
        nonlocal count, now
        count += 1
        with original_locked() as db:
            yield db
        if (stage == "reserve_commit" and count == 1) or (stage == "marker_commit" and count == 2):
            now += 2500

    if stage == "account":
        monkeypatch.setattr(exchange, "account", delayed_account)
    elif stage in {"reserve_commit", "marker_commit"}:
        monkeypatch.setattr(store, "locked", delayed_transaction)
    else:
        original_refresh = engine.refresh_signal_age
        checks = 0

        def delay_at_post(signal):
            nonlocal checks, now
            checks += 1
            if checks == 4:  # The check adjacent to exchange.place, after constructing its body.
                now += 2500
            return original_refresh(signal)

        monkeypatch.setattr(engine, "refresh_signal_age", delay_at_post)
    result = engine.execute(sid)
    assert result["action"] == "expired"
    assert result["signal"]["deadline_ms"] == end + 300000
    assert result["signal"]["age_seconds"] == 301.5
    assert not result["signal"]["executable"] and not exchange.submissions
    orders = store.orders(engine.scope)
    if stage == "account":
        assert not orders
    else:
        assert orders[0]["state"] == "canceled_local"
        assert bool(orders[0]["submit_started"]) == (stage in {"marker_commit", "pre_post"})
        assert "deadline" in store.audit_history(orders[0]["id"])[-1]["reason"]


@pytest.mark.parametrize(
    "payload,code", [({"code": "51603", "data": []}, "51603"), ({"code": "0", "data": []}, None)]
)
def test_http_missing_order_shape_never_releases_unknown(setup_engine, payload, code):
    engine, store, exchange, sid = setup_engine
    exchange.failure = TradingError("uncertain")
    with pytest.raises(TradingError):
        engine.execute(sid)
    client = OKX(engine.settings, httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    exchange.order = client.order
    store.stop()
    for _ in range(4):
        engine.tick()
    row = store.orders(engine.scope)[0]
    assert row["state"] == "unknown" and row["last_error_code"] == code
    with pytest.raises(TradingError, match="禁止人工作废"):
        engine.abandon(row["id"], "测试者", "无法通过空响应或查无判断是否迟到成交", "ABANDON_WITHOUT_RESEND")
    assert len(exchange.submissions) == 1
    client.close()

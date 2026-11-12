from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest

from okx_workbench.execution import Engine
from okx_workbench.market import ExchangeRejected, TradingError


def test_contract_units_caps_and_bounded_ioc(setup_engine):
    engine, store, exchange, sid = setup_engine
    result = engine.execute(sid)
    body = exchange.submissions[0]
    assert result["state"] == "accepted"  # Receipt does not mean filled.
    assert body["ordType"] == "ioc" and body["tdMode"] == "isolated"
    assert body["posSide"] == "net" and not body["reduceOnly"]
    notional = Decimal(body["sz"]) * Decimal("0.01") * Decimal(body["px"])
    assert 0 < notional <= engine.settings.max_order
    assert Decimal(body["px"]) <= Decimal("60000") * Decimal("1.002")
    assert len(body["clOrdId"]) == 32 and body["clOrdId"].isalnum()
    engine.execute(sid)
    assert len(exchange.submissions) == 1
    assert store.orders(engine.scope)[0]["state"] == "filled"


def test_deduplication_survives_new_engine_and_concurrent_calls(setup_engine):
    engine, store, exchange, sid = setup_engine
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda _: engine.execute(sid), range(5)))
    restarted = Engine(engine.settings, store, exchange)
    assert restarted.execute(sid)["action"] == "duplicate"
    assert len(exchange.submissions) == 1
    assert len(store.orders(engine.scope)) == 1


def test_timeout_reserves_intent_and_never_resends(setup_engine):
    engine, store, exchange, sid = setup_engine
    exchange.failure = TimeoutError("secret must never appear in user output")
    with pytest.raises(TradingError, match="禁止重发"):
        engine.execute(sid)
    assert store.orders(engine.scope)[0]["state"] == "unknown"
    exchange.order_state = "live"
    engine.execute(sid)
    assert len(exchange.submissions) == 1
    assert store.orders(engine.scope)[0]["state"] == "live"


def test_unknown_order_lookup_failure_remains_blocked(setup_engine):
    engine, store, exchange, sid = setup_engine
    exchange.failure = TimeoutError()
    with pytest.raises(TradingError):
        engine.execute(sid)

    def not_found(*args):
        raise TradingError("无法确认订单")

    exchange.order = not_found
    with pytest.raises(TradingError):
        engine.execute(sid)
    assert store.orders(engine.scope)[0]["state"] == "unknown"
    assert len(exchange.submissions) == 1


def test_explicit_rejection_is_not_resent_same_signal(setup_engine):
    engine, store, exchange, sid = setup_engine
    exchange.failure = ExchangeRejected("明确拒绝")
    with pytest.raises(TradingError):
        engine.execute(sid)
    assert store.orders(engine.scope)[0]["state"] == "rejected"
    assert engine.execute(sid)["action"] == "duplicate"
    assert len(exchange.submissions) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("pending", [{"ordId": "other"}]),
        ("other_notional", "500"),
        ("available", "0"),
        ("equity", "0"),
        ("position", "-1"),
    ],
)
def test_risk_limits_block_before_submission(setup_engine, field, value):
    engine, store, exchange, sid = setup_engine
    setattr(exchange, field, value)
    with pytest.raises(TradingError):
        engine.execute(sid)
    assert not exchange.submissions and not store.orders(engine.scope)


def test_total_position_and_equity_fraction_cap(setup_engine):
    engine, _, exchange, sid = setup_engine
    exchange.other_notional = "90"
    exchange.equity = "1000"  # 10% cap = 100 USDT, leaving 10 USDT.
    engine.execute(sid)
    body = exchange.submissions[0]
    assert Decimal(body["sz"]) * Decimal("0.01") * Decimal(body["px"]) <= 10


def test_exit_is_reduce_only_and_cannot_open_short(setup_engine):
    engine, _, exchange, sid = setup_engine
    exchange.falling, exchange.position = True, "2"
    engine.execute(sid)
    assert exchange.submissions[0]["side"] == "sell"
    assert exchange.submissions[0]["reduceOnly"] is True
    assert exchange.submissions[0]["sz"] == "2"


def test_pause_prevents_future_orders_and_tick_errors_pause(setup_engine):
    engine, store, exchange, sid = setup_engine
    store.stop()
    with pytest.raises(TradingError, match="未启动"):
        engine.execute(sid)
    assert not exchange.submissions
    engine.start(sid)
    exchange.available = "0"
    engine.tick()
    assert store.strategies()[0]["active"] == 0
    assert store.events()[0]["level"] == "error"


def test_stale_candles_block_and_account_switch_requires_regeneration(setup_engine):
    engine, store, exchange, sid = setup_engine
    candles = exchange.candles("BTC-USDT-SWAP", "1H")
    exchange.candles = lambda *args: [replace(c, ts=c.ts - 10 * 3_600_000) for c in candles]
    with pytest.raises(TradingError, match="过期"):
        engine.execute(sid)
    other = Engine(replace(engine.settings, api_key="another-key"), store, exchange)
    with pytest.raises(TradingError, match="账户已改变"):
        other.start(sid)


def test_offline_buy_close_ledger_with_fees_and_pnl(setup_engine, monkeypatch):
    engine, store, exchange, _ = setup_engine
    engine = Engine(replace(engine.settings, source="offline"), store, exchange)
    report = {
        "spec": {"symbol": "BTC-USDT-SWAP", "bar": "1H", "fast": 4, "slow": 18},
        "validation": {},
        "scope": engine.scope,
    }
    sid = store.add_strategy(report)
    engine.start(sid)
    engine.candles = exchange.candles
    monkeypatch.setattr("okx_workbench.execution.fixture", lambda *args: exchange.candles(args[0], "1H"))
    bought = engine.execute(sid)
    assert bought["state"] == "filled"
    holding = engine.account()
    assert holding["quantity"] > 0 and holding["fees"] > 0
    assert holding["equity"] < 10000  # A same-tick mark reflects cost, never a synthetic gain.
    assert engine.execute(sid)["action"] == "duplicate"
    exchange.falling = True
    sold = engine.execute(sid)
    assert sold["state"] == "filled"
    closed = engine.account()
    assert closed["quantity"] == pytest.approx(0)
    assert closed["equity"] == pytest.approx(10000 + closed["realized"] - closed["fees"])
    assert not exchange.submissions  # Local accounting never calls the exchange.

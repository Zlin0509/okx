from argparse import Namespace
from decimal import Decimal

import pytest
from conftest import FakeOKX

from scripts import demo_smoke


@pytest.mark.parametrize("existing", [False, True])
def test_demo_smoke_preflight_never_places_and_preserves_existing(tmp_path, monkeypatch, existing):
    exchange = FakeOKX()
    exchange.position = "1" if existing else "0"
    monkeypatch.setattr(
        demo_smoke, "credentials", lambda path: {"api_key": "key", "secret": "secret", "passphrase": "pass"}
    )
    captured = []

    def create(settings):
        captured.append(settings)
        return exchange

    monkeypatch.setattr(demo_smoke, "OKX", create)
    result = demo_smoke.run(
        Namespace(credentials_file="unused", database=str(tmp_path / "test.sqlite3"), execute_demo=False)
    )
    assert not exchange.submissions
    assert captured[0].mode == "demo" and captured[0].max_order == Decimal("25")
    assert result["status"] == ("blocked" if existing else "preflight_only")
    assert result["strategy_paused"]
    assert exchange.position == ("1" if existing else "0")


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

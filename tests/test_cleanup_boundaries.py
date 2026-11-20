from decimal import Decimal

import pytest

from okx_workbench.market import TradingError
from scripts import demo_smoke


def configure_cleanup(
    setup_engine,
    monkeypatch,
    owned="0.24",
    price="200",
    minimum="0.01",
    fills=None,
    uncertain=False,
    missing=False,
    external_delta="0",
):
    engine, store, exchange, sid = setup_engine
    exchange.position = owned
    info = {"ctVal": "1", "lotSz": "0.01", "minSz": minimum, "tickSz": "0.1"}
    orders = {}
    original = exchange.place

    def place(body):
        result = original(body)
        quantity = Decimal(body["sz"])
        filled = Decimal(fills.pop(0)) if fills else quantity
        exchange.position = str(Decimal(exchange.position) - filled + Decimal(external_delta))
        orders[body["clOrdId"]] = {
            "state": "canceled" if filled < quantity else "filled",
            "accFillSz": str(filled),
            "clOrdId": body["clOrdId"],
            "ordId": "12345",
        }
        if uncertain:
            raise TimeoutError("mock timeout")
        return result

    def order(symbol, client_id):
        if client_id == "entry":
            return {"state": "filled", "accFillSz": owned}
        if missing:
            raise TradingError("mock missing", "51603")
        return orders[client_id]

    monkeypatch.setattr(exchange, "instrument", lambda *args: info)
    monkeypatch.setattr(exchange, "ticker", lambda *args: {"bidPx": price, "askPx": price})
    monkeypatch.setattr(exchange, "place", place)
    monkeypatch.setattr(exchange, "order", order)
    monkeypatch.setattr(demo_smoke.time, "sleep", lambda *args: None)
    return engine, store, exchange, sid


def invoke(context):
    engine, store, exchange, sid = context
    report = {"symbol": "BTC-USDT-SWAP", "status": "filled_and_position_confirmed"}
    return report, lambda: demo_smoke.cleanup(engine, sid, "entry", Decimal("0"), report)


def test_price_increase_crosses_cap_and_closes_only_owned_legal_chunks(setup_engine, monkeypatch):
    # Entry 0.24 @100 =24 USDT; a doubled quote requires multiple <=25 exits.
    context = configure_cleanup(setup_engine, monkeypatch)
    report, clean = invoke(context)
    clean()
    engine, store, exchange, _ = context
    assert [Decimal(o["sz"]) for o in exchange.submissions] == [Decimal("0.12"), Decimal("0.12")]
    assert sum(Decimal(o["sz"]) for o in exchange.submissions) == Decimal("0.24")
    assert all(o["reduceOnly"] and Decimal(o["sz"]) * Decimal(o["px"]) <= 25 for o in exchange.submissions)
    assert report["cleanup_complete"] and report["position_after_cleanup"] == "0.00"
    assert report["status"] == "filled_and_position_confirmed"
    assert all(not s["active"] for s in store.strategies())
    assert all(o["state"] == "filled" for o in store.orders(engine.scope))


@pytest.mark.parametrize(
    "owned,minimum,price,expected",
    [
        ("0.07", "0.025", "500", ["0.04", "0.03"]),
        ("0.10", "0.03", "600", ["0.04", "0.03", "0.03"]),
        ("0.06", "0.03", "800", ["0.03", "0.03"]),
    ],
)
def test_legal_partition_avoids_untradeable_dust(setup_engine, monkeypatch, owned, minimum, price, expected):
    context = configure_cleanup(setup_engine, monkeypatch, owned, price, minimum)
    report, clean = invoke(context)
    clean()
    assert [Decimal(o["sz"]) for o in context[2].submissions] == list(map(Decimal, expected))
    assert report["cleanup_complete"]


@pytest.mark.parametrize(
    "owned,minimum,price", [("0.05", "0.03", "600"), ("0.01", "0.01", "2600"), ("0.035", "0.01", "200")]
)
def test_impossible_or_unrounded_partition_stops_before_post(
    setup_engine, monkeypatch, owned, minimum, price
):
    context = configure_cleanup(setup_engine, monkeypatch, owned, price, minimum)
    report, clean = invoke(context)
    with pytest.raises(TradingError, match="清理未完成"):
        clean()
    assert not context[2].submissions
    assert report["status"] == "cleanup_incomplete" and report["passed"] is False
    assert not report["cleanup_complete"] and context[2].position == owned


def test_terminal_partial_fill_replans_exact_verified_remainder(setup_engine, monkeypatch):
    context = configure_cleanup(setup_engine, monkeypatch, fills=["0.05"])
    report, clean = invoke(context)
    clean()
    assert [Decimal(o["sz"]) for o in context[2].submissions] == list(map(Decimal, ["0.12", "0.12", "0.07"]))
    assert report["cleanup_orders"][0]["state"] == "canceled"
    assert Decimal(report["confirmed_closed_contracts"]) == Decimal("0.24")


def test_partial_fill_that_leaves_illegal_remainder_reports_incomplete(setup_engine, monkeypatch):
    context = configure_cleanup(setup_engine, monkeypatch, "0.06", "800", "0.03", fills=["0.02"])
    report, clean = invoke(context)
    with pytest.raises(TradingError, match="无法|不能"):
        clean()
    assert len(context[2].submissions) == 1
    assert report["remaining_proven_contracts"] == "0.04" and not report["cleanup_complete"]
    assert report["status"] == "cleanup_incomplete" and report["passed"] is False


def test_timeout_resolved_by_terminal_lookup_never_resends_same_intent(setup_engine, monkeypatch):
    context = configure_cleanup(setup_engine, monkeypatch, uncertain=True)
    report, clean = invoke(context)
    clean()
    ids = [o["clOrdId"] for o in context[2].submissions]
    assert len(ids) == len(set(ids)) == 2 and report["cleanup_complete"]


def test_unknown_timeout_and_missing_lookup_stops_all_further_posts(setup_engine, monkeypatch):
    context = configure_cleanup(setup_engine, monkeypatch, uncertain=True, missing=True)
    report, clean = invoke(context)
    with pytest.raises(TradingError, match="终态不明"):
        clean()
    assert len(context[2].submissions) == 1
    order = context[1].orders(context[0].scope)[0]
    assert order["state"] == "unknown" and order["submit_started"] is not None
    assert report["status"] == "cleanup_incomplete" and not report["cleanup_complete"]
    assert report["cleanup_intents"][0]["state"] == "unknown"
    assert "…" in report["cleanup_intents"][0]["client_id_masked"]
    with pytest.raises(TradingError):
        clean()
    assert len(context[2].submissions) == 1


def test_external_position_delta_stops_before_next_chunk(setup_engine, monkeypatch):
    context = configure_cleanup(setup_engine, monkeypatch, external_delta="0.01")
    report, clean = invoke(context)
    with pytest.raises(TradingError, match="不符"):
        clean()
    assert len(context[2].submissions) == 1 and not report["cleanup_complete"]


def test_unknown_entry_is_neither_canceled_nor_closed(setup_engine, monkeypatch):
    context = configure_cleanup(setup_engine, monkeypatch)
    monkeypatch.setattr(context[2], "order", lambda *args: {"state": "unknown", "accFillSz": "0"})
    context[2].cancel = lambda *args: pytest.fail("unknown entry canceled")
    report, clean = invoke(context)
    with pytest.raises(TradingError, match="状态不明"):
        clean()
    assert not context[2].submissions and report["status"] == "cleanup_incomplete"


def test_overreported_fill_keeps_intent_unknown(setup_engine, monkeypatch):
    context = configure_cleanup(setup_engine, monkeypatch, fills=["0.13"])
    report, clean = invoke(context)
    with pytest.raises(TradingError, match="成交量"):
        clean()
    assert len(context[2].submissions) == 1 and not report["cleanup_complete"]
    assert context[1].orders(context[0].scope)[0]["state"] == "unknown"

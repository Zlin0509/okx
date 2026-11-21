import sqlite3
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from okx_workbench.app import create_app
from okx_workbench.config import BARS, Settings
from okx_workbench.history import DAY_MS, Collector, History
from okx_workbench.market import OKX, TradingError
from okx_workbench.strategy import StrategySpec

PAIR = ("BTC-USDT-SWAP", "1m")
STEP = BARS[PAIR[1]]
CUT = 1_800_000_000_000


def candle(ts, close=101, volume=10, confirmed=True):
    return [
        str(ts),
        "100",
        str(max(102, close)),
        "99",
        str(close),
        str(volume),
        "0",
        "0",
        str(int(confirmed)),
    ]


@pytest.fixture
def clock(monkeypatch):
    current = [CUT + 5000]
    monkeypatch.setattr("okx_workbench.history.now_ms", lambda: current[0])
    return current


def test_restart_and_merge_confirmed_candles(tmp_path, clock):
    path = tmp_path / "market.sqlite3"
    history = History(path)
    history.subscribe(*PAIR)
    history.ingest(*PAIR, [candle(CUT, 104, 20, False), candle(CUT - STEP)])
    history.ingest(*PAIR, [candle(CUT, 103, 10, False), candle(CUT - STEP, 110)])
    assert [r[4:] for r in history.rows(*PAIR, confirmed=False)] == [(101.0, 10.0, 1), (104.0, 20.0, 0)]
    assert len(history.series(*PAIR)) == 1
    assert not Collector(History(path), object()).fresh(*PAIR, CUT - STEP)
    clock[0] += STEP
    history.ingest(*PAIR, [candle(CUT, 109, 21), candle(CUT, 104, 20, False)])
    restarted = History(path)
    assert [c.close for c in restarted.series(*PAIR)] == [101, 109]
    assert PAIR in restarted.pairs()


def test_retention_reclaims_file_and_preserves_ledger(tmp_path, clock):
    ledger = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE orders (id INTEGER)")
        db.execute("INSERT INTO orders VALUES (42)")
    with pytest.raises(TradingError, match="非行情数据库"):
        History(ledger)

    history = History(tmp_path / "cache.sqlite3", max_rows=8000)
    history.subscribe(*PAIR)
    for pos in range(0, 7200, 300):
        history.ingest(*PAIR, [candle(CUT - (i + 1) * STEP) for i in range(pos, pos + 300)])
    before = history.disk_bytes()
    smaller = History(history.path, max_rows=301, days=1)
    assert len(smaller.series(*PAIR)) == 301
    assert smaller.disk_bytes() < before / 2
    clock[0] += 2 * DAY_MS
    smaller.unsubscribe(*PAIR)
    # Maintenance runs even with zero subscriptions.
    smaller.unsubscribe("BTC-USDT-SWAP", "1H")
    Collector(smaller, object()).poll()
    assert smaller.rows(*PAIR) == []
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT id FROM orders").fetchall() == [(42,)]
    assert not history.path.with_name("cache.sqlite3-wal").exists()


def test_disk_budget_evicts_oldest_surplus_and_limits_subscriptions(tmp_path, clock):
    history = History(tmp_path / "cache.sqlite3", max_rows=50000, budget=4 * 1024 * 1024, max_pairs=2)
    history.subscribe(*PAIR)
    history.unsubscribe("BTC-USDT-SWAP", "1H")
    other = ("ETH-USDT-SWAP", "1m")
    history.subscribe(*other)
    with pytest.raises(TradingError, match="订阅已达上限"):
        history.subscribe("ETH-USDT-SWAP", "4H")
    for offset in range(36000, 0, -300):
        history.ingest(
            *PAIR, [candle(CUT - i * STEP, 100.5 + i / 100000) for i in range(offset, offset - 300, -1)]
        )
        assert history.disk_bytes() <= history.budget // 3
    history.ingest(*other, [candle(CUT - i * STEP) for i in range(1, 301)])
    assert 300 <= len(history.rows(*PAIR, limit=50000)) < 36000
    assert history.rows(*PAIR, limit=1)[0][0] == CUT - STEP
    assert len(history.series(*other)) == 300
    with history.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE candles SET v=17")
        assert history.path.with_name("cache.sqlite3-journal").stat().st_size > 0
        assert history.disk_bytes() < history.budget
        db.rollback()


class Feed:
    def __init__(self, clock):
        self.clock = clock
        self.fail = False
        self.requests = []

    def request(self, method, path, params):
        assert method == "GET" and path.startswith("/api/v5/market/")
        self.requests.append((path, params))
        if self.fail:
            raise TradingError("模拟断线")
        step = BARS[params["bar"]]
        end = self.clock[0] // step * step
        if path.endswith("/history-candles"):
            return [candle(params["after"] - step * (i + 1)) for i in range(params["limit"])]
        return [candle(end, 104, confirmed=False), candle(end - step), candle(end - 2 * step)]

    def close(self):
        pass


def test_reconnect_backfills_multi_page_gap_and_reuses_local_window(tmp_path, clock, monkeypatch):
    cache = History(tmp_path / "cache.sqlite3")
    cache.subscribe(*PAIR)
    elapsed = [10]
    monkeypatch.setattr("okx_workbench.history.time.monotonic", lambda: elapsed[0])
    feed = Feed(clock)
    collector = Collector(cache, feed)
    collector.sync(*PAIR)
    assert len(collector.candles(*PAIR, minimum=300, signal=True)) == 300
    assert len(feed.requests) == 2
    collector.candles(*PAIR, minimum=130)
    assert len(feed.requests) == 2  # Strategy/backtest read the persisted data.
    collector.sync(*PAIR)
    assert len(feed.requests) == 3 and feed.requests[-1][1]["limit"] == 3

    feed.fail = True
    collector.sync(*PAIR)
    assert not collector.fresh(*PAIR, CUT - STEP)
    with pytest.raises(TradingError, match="断连"):
        collector.candles(*PAIR, minimum=20, signal=True)
    assert len(collector.candles(*PAIR, minimum=130)) >= 300
    collector.sync(*PAIR)
    assert len(feed.requests) == 4  # Cooldown caps retry requests.
    clock[0] += 650 * STEP
    elapsed[0] += 6
    feed.fail = False
    collector.sync(*PAIR)
    assert collector.fresh(*PAIR, CUT + 649 * STEP)
    assert len(cache.series(*PAIR)) >= 950
    assert len(feed.requests) == 8  # Latest request plus three gap pages.
    clock[0] += STEP
    assert not collector.fresh(*PAIR, CUT + 649 * STEP)  # A new bar closed before refresh.


def test_disconnect_during_execution_preflight_blocks_post(setup_engine, tmp_path, clock, monkeypatch):
    engine, _, exchange, sid = setup_engine
    cache = History(tmp_path / "cache.sqlite3")
    feed = Feed(clock)
    engine.history = Collector(cache, feed)
    spec = StrategySpec()
    cache.subscribe(spec.symbol, spec.bar)
    engine.history.sync(spec.symbol, spec.bar)
    # Make a long signal from confirmed local bars.
    monkeypatch.setattr(
        "okx_workbench.execution.target",
        lambda bars, spec: {
            "target": "long",
            "candle_ts": bars[-1].ts,
            "close": bars[-1].close,
        },
    )
    monkeypatch.setattr(engine, "now_ms", lambda: clock[0])
    original_ticker = exchange.ticker

    def drop_network(*args):
        feed.fail = True
        engine.history.sync(spec.symbol, spec.bar)
        return original_ticker(*args)

    monkeypatch.setattr(exchange, "ticker", drop_network)
    assert engine.execute(sid)["signal"]["market_unavailable"]
    assert not exchange.submissions


def test_public_transport_does_not_sign_requests(tmp_path, clock):
    signed_headers = []

    def respond(request):
        signed_headers.extend(k for k in request.headers if k.lower().startswith("ok-access"))
        rows = (
            [candle(CUT - i * STEP) for i in range(1, 301)]
            if "history-candles" in str(request.url)
            else [candle(CUT, confirmed=False), candle(CUT - STEP), candle(CUT - 2 * STEP)]
        )
        return httpx.Response(200, json={"code": "0", "data": rows})

    with OKX(Settings(), transport=httpx.MockTransport(respond)).http as client:
        public = OKX(Settings())
        public.http.close()
        public.http = client
        cache = History(tmp_path / "cache.sqlite3")
        cache.subscribe(*PAIR)
        Collector(cache, public).sync(*PAIR)
        assert len(cache.series(*PAIR)) == 300
        assert not signed_headers


def test_public_research_routes_bootstrap_and_collect_independently(tmp_path, clock, monkeypatch):
    class ObservedFeed(Feed):
        recovered = threading.Event()

        def request(self, method, path, params):
            rows = super().request(method, path, params)
            if path.endswith("/history-candles"):
                self.recovered.set()
            return rows

    def deny_private(*args, **kwargs):
        raise AssertionError("public research must not request the private client")

    monkeypatch.setattr(OKX, "request", deny_private)
    feed = ObservedFeed(clock)
    settings = Settings(source="okx", history_enabled=True, database=str(tmp_path / "ledger.sqlite3"))
    with TestClient(create_app(settings, public_market=feed)) as client:
        assert feed.recovered.wait(2)
        for _ in range(200):
            response = client.get("/api/state").json()
            if response["history"]["subscriptions"][0]["connected"]:
                break
            time.sleep(0.01)
        assert response["history"]["subscriptions"][0]["connected"]
        generated = client.post("/api/strategies/generate", json={})
        assert generated.status_code == 200, generated.text
        assert generated.json()["scope"] is None
        assert client.post("/api/backtests", json={}).status_code == 200
        assert client.get("/api/account").json()["source"] == "okx_public"
        strategy = generated.json()["id"]
        assert client.post(f"/api/strategies/{strategy}/tick", json={}).status_code == 409
        assert client.post("/api/stop", json={}).status_code == 200
        assert client.get("/api/state").json()["history"]["subscriptions"][0]["connected"]
        candles = client.get("/api/market").json()["candles"]
        assert candles and candles[-1]["confirmed"] == 0
        assert client.post("/api/market/subscriptions", json={"bar": "4H"}).status_code == 200
        assert client.post("/api/market/unsubscribe", json={"bar": "4H"}).status_code == 200

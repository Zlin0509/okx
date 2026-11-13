import time
from dataclasses import replace
from pathlib import Path

import pytest

from okx_workbench.config import Settings
from okx_workbench.execution import Engine
from okx_workbench.market import Candle
from okx_workbench.store import Store


def rising_candles(count=200, falling=False):
    end = int(time.time() * 1000) // 3_600_000 * 3_600_000
    result = []
    for i in range(count):
        price = 60_000 + (count - i if falling else i) * 10
        result.append(Candle(end - (count - i) * 3_600_000, price, price + 20, price - 20, price + 5))
    return result


class FakeOKX:
    def __init__(self):
        self.submissions = []
        self.failure = None
        self.position = "0"
        self.other_notional = "0"
        self.pending = []
        self.equity = "10000"
        self.available = "10000"
        self.order_state = "filled"
        self.falling = False
        self.uid = "12345678"

    def identity(self):
        return self.uid

    def candles(self, symbol, bar):
        return rising_candles(falling=self.falling)

    def account(self):
        positions = [
            {
                "instId": "BTC-USDT-SWAP",
                "pos": self.position,
                "posSide": "net",
                "mgnMode": "isolated",
                "notionalUsd": str(float(self.position) * 600),
            }
        ]
        if float(self.other_notional):
            positions.append({"instId": "ETH-USDT-SWAP", "pos": "1", "notionalUsd": self.other_notional})
        return {
            "equity": self.equity,
            "available": self.available,
            "unrealized_pnl": "0",
            "positions": positions,
            "pending": self.pending,
        }

    def instrument(self, symbol):
        return {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}

    def ticker(self, symbol):
        return {"askPx": "60000", "bidPx": "59990"}

    def place(self, body):
        self.submissions.append(body)
        if self.failure:
            raise self.failure
        return {"ordId": "12345", "sCode": "0"}

    def order(self, symbol, client_id):
        return {"state": self.order_state, "ordId": "12345", "accFillSz": "0.16"}

    def close(self):
        pass


@pytest.fixture
def setup_engine(tmp_path):
    settings = replace(
        Settings(),
        source="okx",
        api_key="test-key",
        secret="test-secret",
        passphrase="test-pass",
        database=str(tmp_path / "test.sqlite3"),
    )
    store = Store(settings.database)
    exchange = FakeOKX()
    engine = Engine(settings, store, exchange)
    report = {
        "spec": {"symbol": "BTC-USDT-SWAP", "bar": "1H", "fast": 4, "slow": 18},
        "validation": {},
        "scope": engine.scope,
        "source": "okx",
    }
    strategy_id = store.add_strategy(report)
    engine.start(strategy_id)
    return engine, store, exchange, strategy_id


@pytest.fixture(autouse=True)
def isolated_host_and_fresh_clock(tmp_path, monkeypatch):
    # Never share coordination state with a real local account, or depend on wall-clock minutes.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(Engine, "now_ms", lambda self: int(time.time() // 3600) * 3_600_000 + 60_000)

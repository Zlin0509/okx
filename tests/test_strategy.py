from dataclasses import replace

import pytest
from pydantic import ValidationError

from okx_workbench.market import Candle, TradingError, fixture
from okx_workbench.strategy import StrategySpec, backtest, generate, target


@pytest.mark.parametrize(
    "fields",
    [
        {"fast": 24, "slow": 8},
        {"symbol": "BTC-USD-SWAP"},
        {"bar": "1m"},
        {"allocation": float("nan")},
        {"allocation": 0.2},
    ],
)
def test_invalid_strategy(fields):
    with pytest.raises(ValidationError):
        StrategySpec(**fields)


def test_backtest_next_open_and_pnl_accounting():
    candles = [Candle(i * 3_600_000, 100 + i, 120 + i, 90 + i, 101 + i) for i in range(15)]
    candles[3] = Candle(candles[3].ts, 200, 210, 100, 104)
    report = backtest(candles, StrategySpec(fast=2, slow=3), fee_bps=5, slip_bps=10)
    assert report["trades"][0]["entry"] == pytest.approx(200 * 1.001)
    assert report["net_pnl"] == pytest.approx(sum(t["pnl"] for t in report["trades"]))
    assert report["fees"] > 0
    assert report["curve"][-1]["equity"] == report["final_equity"]


def test_no_future_prices_used_in_prior_curve_or_selection():
    candles = fixture(end=1_770_000_000_000)
    modified = [
        replace(c, open=c.open * 2, high=c.high * 2, low=c.low * 2, close=c.close * 2) if i >= 250 else c
        for i, c in enumerate(candles)
    ]
    spec = StrategySpec()
    original, changed = backtest(candles, spec), backtest(modified, spec)
    assert original["curve"][:200] == changed["curve"][:200]
    first, second = generate(candles, spec), generate(modified, spec)
    assert first["spec"] == second["spec"]
    assert first["training"]["curve"] == second["training"]["curve"]
    assert first["training"]["trades"] == second["training"]["trades"]
    assert len(first["validation"]["curve"]) == first["validation_bars"]


def test_flat_prices_do_not_trade():
    candles = [Candle(i * 3_600_000, 100, 100, 100, 100) for i in range(100)]
    assert target(candles, StrategySpec())["target"] == "flat"
    report = backtest(candles, StrategySpec())
    assert report["trade_count"] == 0
    assert report["net_pnl"] == 0
    assert report["max_drawdown_pct"] == 0


def test_gap_or_bad_price_fails_closed():
    candles = fixture()
    with pytest.raises(TradingError):
        backtest(candles[:20] + candles[21:], StrategySpec())
    candles[-1] = replace(candles[-1], close=float("nan"))
    with pytest.raises(TradingError):
        generate(candles, StrategySpec())


def test_synthetic_timeframes_share_prices_and_contain_hourly_extremes():
    end = 1_770_000_000_000 // 86_400_000 * 86_400_000
    hourly = fixture(bar="1H", limit=24, end=end)
    daily = fixture(bar="1D", limit=1, end=end)[0]
    assert daily.open == hourly[0].open
    assert daily.close == hourly[-1].close
    assert daily.high >= max(c.high for c in hourly)
    assert daily.low <= min(c.low for c in hourly)

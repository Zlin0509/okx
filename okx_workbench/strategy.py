import math
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import BARS, SYMBOLS
from .market import Candle, TradingError


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = "BTC-USDT-SWAP"
    bar: str = "1H"
    fast: int = Field(default=8, ge=2, le=40)
    slow: int = Field(default=24, ge=3, le=100)
    allocation: float = Field(default=0.05, gt=0, le=0.1)

    @model_validator(mode="after")
    def validate_spec(self):
        if self.symbol not in SYMBOLS or self.bar not in BARS or self.fast >= self.slow:
            raise ValueError("仅支持 BTC/ETH USDT 永续与 1H/4H/1D，快均线必须小于慢均线")
        return self


class BacktestRequest(StrategySpec):
    initial_capital: float = Field(default=10_000, ge=100, le=10_000_000)
    fee_bps: float = Field(default=5, ge=0, le=100)
    slippage_bps: float = Field(default=2, ge=0, le=100)


def validate_candles(candles, bar):
    if len(candles) < 10:
        raise TradingError("可用 K 线不足")
    previous = None
    for candle in candles:
        if (
            not all(math.isfinite(v) and v > 0 for v in (candle.open, candle.high, candle.low, candle.close))
            or candle.low > min(candle.open, candle.close)
            or candle.high < max(candle.open, candle.close)
        ):
            raise TradingError("K 线价格无效")
        if previous is not None and candle.ts - previous != BARS[bar]:
            raise TradingError("K 线缺失或顺序错误；暂停生成交易信号")
        previous = candle.ts


def target(candles: list[Candle], spec: StrategySpec):
    if len(candles) < spec.slow:
        raise TradingError("K 线数量不足以计算慢均线")
    closes = [c.close for c in candles]
    fast = sum(closes[-spec.fast :]) / spec.fast
    slow = sum(closes[-spec.slow :]) / spec.slow
    return {
        "target": "long" if fast > slow else "flat",
        "fast_ma": fast,
        "slow_ma": slow,
        "candle_ts": candles[-1].ts,
        "close": closes[-1],
    }


def backtest(candles: list[Candle], spec: StrategySpec, initial=10_000, fee_bps=5, slip_bps=2, trade_start=0):
    validate_candles(candles, spec.bar)
    if len(candles) < spec.slow + 2:
        raise TradingError("回测样本不足")
    fee, slip = fee_bps / 10_000, slip_bps / 10_000
    cash, quantity, entry, entry_fee = float(initial), 0.0, 0.0, 0.0
    trades, curve, fees = [], [], 0.0

    def close_position(price, ts, reason):
        nonlocal cash, quantity, fees
        exit_price = price * (1 - slip)
        exit_fee = quantity * exit_price * fee
        pnl = quantity * (exit_price - entry) - entry_fee - exit_fee
        cash += quantity * exit_price - exit_fee
        fees += exit_fee
        trades.append(
            {"ts": ts, "entry": entry, "exit": exit_price, "quantity": quantity, "pnl": pnl, "reason": reason}
        )
        quantity = 0.0

    # Signal at t-1 close; all normal orders execute at t open.
    for i in range(max(spec.slow, trade_start), len(candles)):
        c = candles[i]
        wanted = target(candles[:i], spec)["target"]
        if wanted == "long" and quantity == 0:
            entry = c.open * (1 + slip)
            budget = cash * spec.allocation
            quantity = budget / (entry * (1 + fee))
            entry_fee = quantity * entry * fee
            cash -= quantity * entry + entry_fee
            fees += entry_fee
        elif wanted == "flat" and quantity > 0:
            close_position(c.open, c.ts, "signal")
        curve.append({"ts": c.ts, "equity": cash + quantity * c.close})
    if quantity:
        close_position(candles[-1].close, candles[-1].ts, "end_of_test")
        curve[-1]["equity"] = cash
    peak, drawdown = float(initial), 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, (peak - point["equity"]) / peak)
    return {
        "initial_capital": initial,
        "final_equity": cash,
        "net_pnl": cash - initial,
        "return_pct": (cash / initial - 1) * 100,
        "max_drawdown_pct": drawdown * 100,
        "win_rate_pct": sum(t["pnl"] > 0 for t in trades) / len(trades) * 100 if trades else 0,
        "trade_count": len(trades),
        "fees": fees,
        "curve": curve,
        "trades": trades,
        "assumptions": "做多/空仓，信号后下一根开盘成交；末尾强制平仓。含双边手续费和滑点，不含资金费、清算及盘口深度。理论数量未按交易所张数取整。",
    }


def generate(candles, base: StrategySpec):
    """Small, explainable grid search on training only; untouched chronological holdout."""
    validate_candles(candles, base.bar)
    split = int(len(candles) * 0.7)
    if split < 90 or len(candles) - split < 30:
        raise TradingError("自动生成至少需要 130 根连续 K 线")
    candidates = []
    for fast in (4, 8, 12):
        for slow in (18, 24, 36):
            spec = base.model_copy(update={"fast": fast, "slow": slow})
            report = backtest(candles[:split], spec)
            # Penalize drawdown, do not reward the unused validation partition.
            score = report["return_pct"] - report["max_drawdown_pct"]
            candidates.append((score, spec, report))
    score, chosen, train = max(candidates, key=lambda c: c[0])
    validation = backtest(candles, chosen, trade_start=split)
    return {
        "spec": chosen.model_dump(),
        "training": train,
        "validation": validation,
        "score": score,
        "train_bars": split,
        "validation_bars": len(candles) - split,
        "method": "9 组均线候选；前 70% 按收益减回撤选参，后 30% 独立验证。结果不代表未来表现。",
    }


def serialize_candles(candles):
    return [asdict(c) for c in candles]

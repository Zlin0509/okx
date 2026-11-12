"""Explicitly authorized one-shot demo smoke. Never starts a background runner."""

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

# Allows `uv run python scripts/demo_smoke.py` from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from okx_workbench.config import Settings  # noqa: E402
from okx_workbench.execution import TERMINAL, Engine, quantize  # noqa: E402
from okx_workbench.market import OKX, TradingError, number  # noqa: E402
from okx_workbench.store import Store  # noqa: E402
from okx_workbench.strategy import StrategySpec, backtest, target  # noqa: E402


def credentials(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        raise TradingError("凭据文件必须属于当前用户且仅当前用户可读写")
    payload = json.loads(path.read_text())
    return {
        "api_key": payload["api_key"],
        "secret": payload["secret_key"],
        "passphrase": payload["passphrase"],
    }


def position(account, symbol):
    return sum(
        (number(p.get("pos") or "0") for p in account["positions"] if p["instId"] == symbol), Decimal(0)
    )


def confirm(exchange, store, scope, symbol, client_id):
    for _ in range(10):
        data = exchange.order(symbol, client_id)
        state = data.get("state", "unknown")
        if state in TERMINAL:
            with store.locked() as db:
                db.execute(
                    "UPDATE orders SET state=?,filled=?,updated=? WHERE id=?",
                    (state, str(number(data.get("accFillSz") or "0")), time.time(), client_id),
                )
            return data
        time.sleep(0.5)
    raise TradingError("订单尚未结束，停止后续订单；需人工核对")


def summarize_order(data):
    return {
        "state": data.get("state"),
        "filled_contracts": data.get("accFillSz"),
        "average_price": data.get("avgPx"),
        "fee": data.get("fee"),
        "fee_currency": data.get("feeCcy"),
        "updated_ms": data.get("uTime"),
    }


def cleanup(engine, sid, entry_id, maximum_filled, summary):
    """Reduce only the proven new position; preserve any pre-existing or concurrent work."""
    exchange, store = engine.exchange, engine.store
    symbol = summary["symbol"]
    engine.store.stop()
    entry = exchange.order(symbol, entry_id)
    if entry.get("state") not in TERMINAL:
        exchange.cancel(symbol, entry_id)
        entry = confirm(exchange, store, engine.scope, symbol, entry_id)
    maximum_filled = max(maximum_filled, number(entry.get("accFillSz") or "0"))
    summary["entry_order"] = summarize_order(entry)
    summary["cleanup_orders"] = []
    for attempt in range(3):
        account = exchange.account()
        owned = position(account, symbol)
        if owned == 0:
            break
        if owned < 0 or owned > maximum_filled:
            raise TradingError("持仓与本次已确认新增量不符，拒绝接管其他交易")
        if account["pending"]:
            raise TradingError("检测到并发挂单，停止清理以免影响其他交易")
        info = exchange.instrument(symbol)
        quantity = quantize(owned, number(info["lotSz"]))
        if quantity != owned:
            raise TradingError("新增持仓不能按合约单位平仓，需人工核对")
        quote = exchange.ticker(symbol)
        price = quantize(
            number(quote["bidPx"]) * (1 - engine.settings.slippage_bps / 10_000),
            number(info["tickSz"]),
            ROUND_CEILING,
        )
        client_id = "wc" + hashlib.sha256(f"{entry_id}:{attempt}".encode()).hexdigest()[:30]
        now = time.time()
        with store.locked() as db:
            db.execute(
                """INSERT INTO orders(id,scope,strategy_id,symbol,side,quantity,price,state,created,updated)
                          VALUES(?,?,?,?,'sell',?,?,'reserved',?,?)""",
                (client_id, engine.scope, sid, symbol, str(quantity), str(price), now, now),
            )
        body = {
            "instId": symbol,
            "tdMode": "isolated",
            "posSide": "net",
            "side": "sell",
            "ordType": "ioc",
            "sz": str(quantity),
            "px": str(price),
            "reduceOnly": True,
            "clOrdId": client_id,
        }
        try:
            exchange.place(body)
        except TradingError:
            # Unknown receipt is resolved only by GET; never resend the same order.
            with store.locked() as db:
                db.execute("UPDATE orders SET state='unknown',updated=? WHERE id=?", (time.time(), client_id))
        closed = confirm(exchange, store, engine.scope, symbol, client_id)
        summary["cleanup_orders"].append(summarize_order(closed))
        time.sleep(0.5)
    account = exchange.account()
    summary["position_after_cleanup"] = str(position(account, symbol))
    summary["pending_orders_after_cleanup"] = len(account["pending"])
    summary["strategy_paused"] = all(not s["active"] for s in store.strategies())
    if position(account, symbol) != 0 or account["pending"]:
        raise TradingError("清理后仍有持仓或挂单，需人工核对")


def run(args):
    settings = Settings(
        mode="demo",
        source="okx",
        **credentials(Path(args.credentials_file)),
        max_order=Decimal("25"),
        max_position=Decimal("25"),
        equity_fraction=Decimal("0.001"),
        database=args.database,
    )
    exchange = OKX(settings)
    store, engine = Store(settings.database), None
    summary = {
        "environment": "OKX demo",
        "simulated_header": True,
        "started_at": datetime.now(UTC).isoformat(),
        "order_cap_usdt": "25",
        "executed": False,
    }
    sid, entry_id, proven_fill = None, None, Decimal(0)
    try:
        engine = Engine(settings, store, exchange)
        store.stop()
        account = exchange.account()
        summary.update(
            {
                "authentication": "ok",
                "position_mode": "net_mode",
                "existing_positions": sum(number(p.get("pos") or "0") != 0 for p in account["positions"]),
                "existing_pending_orders": len(account["pending"]),
            }
        )
        # Stricter than the production engine: this smoke requires an entirely idle SWAP account.
        if summary["existing_positions"] or account["pending"]:
            raise TradingError("模拟账户已有持仓或挂单，本次测试停止且不修改它们")
        if number(account["available"]) < 25:
            raise TradingError("模拟账户可用 USDT 不足 25")
        selected = None
        for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
            info = exchange.instrument(symbol)
            for bar in ("1H", "4H", "1D"):
                candles = engine.candles(symbol, bar)
                for fast in (2, 4, 8, 12, 24, 36):
                    for slow in (3, 6, 18, 24, 36, 60, 100):
                        if fast >= slow:
                            continue
                        spec = StrategySpec(symbol=symbol, bar=bar, fast=fast, slow=slow, allocation=0.001)
                        signal = target(candles, spec)
                        if signal["target"] == "long":
                            selected = spec, signal, candles, info
                            break
                    if selected:
                        break
                if selected:
                    break
            if selected:
                break
        if selected is None:
            raise TradingError("已确认 K 线没有有效做多信号，本次不下单")
        spec, signal, candles, info = selected
        summary.update(
            {
                "symbol": spec.symbol,
                "strategy": spec.model_dump(),
                "signal": signal,
                "contract_spec": {k: info[k] for k in ("ctVal", "lotSz", "minSz", "tickSz")},
                "position_before": "0",
                "market": "OKX confirmed candles",
            }
        )
        if not args.execute_demo:
            summary["status"] = "preflight_only"
            return summary
        report = {
            "spec": spec.model_dump(),
            "source": "okx",
            "scope": engine.scope,
            "validation": backtest(candles, spec),
        }
        sid = store.add_strategy(report)
        engine.start(sid)
        summary["triggered_at"] = datetime.now(UTC).isoformat()
        try:
            result = engine.execute(sid)  # The same real signal -> risk -> durable intent -> IOC chain as UI.
            entry_id = result.get("id")
        finally:
            store.stop()
            own = [o for o in store.orders(engine.scope) if o["strategy_id"] == sid and o["side"] == "buy"]
            if own:
                entry_id = own[0]["id"]
        if not entry_id:
            raise TradingError("信号未产生新订单，本次未成交")
        entry = confirm(exchange, store, engine.scope, spec.symbol, entry_id)
        proven_fill = number(entry.get("accFillSz") or "0")
        summary["entry_order"] = summarize_order(entry)
        summary["direction"] = "buy / long"
        summary["submitted_contracts"] = next(
            o["quantity"] for o in store.orders(engine.scope) if o["id"] == entry_id
        )
        summary["executed"] = proven_fill > 0
        for _ in range(6):
            current = position(exchange.account(), spec.symbol)
            if current == proven_fill:
                break
            time.sleep(0.5)
        summary["position_after_entry"] = str(current)
        if proven_fill <= 0 or current != proven_fill:
            raise TradingError("订单未成交或持仓变化尚未确认")
        summary["status"] = "filled_and_position_confirmed"
    except TradingError as exc:
        summary.update({"status": "blocked", "error": str(exc), "error_code": exc.code})
    except Exception as exc:
        summary.update({"status": "blocked", "error_type": type(exc).__name__})
    finally:
        store.stop()
        if engine and entry_id:
            try:
                cleanup(engine, sid, entry_id, proven_fill, summary)
                summary["cleanup"] = "complete"
            except TradingError as exc:
                summary.update(
                    {"cleanup": "blocked", "cleanup_error": str(exc), "cleanup_error_code": exc.code}
                )
            except Exception as exc:
                summary.update({"cleanup": "blocked", "cleanup_error_type": type(exc).__name__})
        summary["strategy_paused"] = True
        exchange.close()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument(
        "--execute-demo", action="store_true", help="Actually submit up to 25 USDT to demo only"
    )
    parser.add_argument("--database", default="data/demo-smoke.sqlite3")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()
    try:
        summary = run(args)
    except TradingError as exc:
        summary = {"status": "blocked", "error": str(exc), "error_code": exc.code}
    except Exception as exc:
        summary = {"status": "blocked", "error_type": type(exc).__name__}
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(output + "\n")
    print(output)
    return (
        0
        if summary.get("status") in {"preflight_only", "filled_and_position_confirmed"}
        and summary.get("cleanup") != "blocked"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())

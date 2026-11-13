"""Explicitly authorized one-shot demo smoke. Never starts a background runner."""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

# Allows `uv run python scripts/demo_smoke.py` from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from okx_workbench.config import BARS, Settings  # noqa: E402
from okx_workbench.execution import TERMINAL, Engine, quantize  # noqa: E402
from okx_workbench.market import OKX, ExchangeRejected, TradingError, number  # noqa: E402
from okx_workbench.store import Store  # noqa: E402
from okx_workbench.strategy import StrategySpec, backtest, target  # noqa: E402


def credentials(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise TradingError(
            "指定模拟盘凭据文件不存在；需 api_key、secret_key、passphrase，读取+交易且无提现权限，并配置所需 IP 白名单"
        ) from None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        raise TradingError("凭据文件必须属于当前用户且仅当前用户可读写")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str) or not payload[key].strip()
        for key in ("api_key", "secret_key", "passphrase")
    ):
        raise TradingError("模拟盘凭据字段缺失或为空")
    return {
        "api_key": payload["api_key"],
        "secret": payload["secret_key"],
        "passphrase": payload["passphrase"],
    }


def mask_id(value):
    value = str(value or "")
    return value[:4] + "…" + value[-4:] if len(value) > 8 else "***"


def check_permissions(exchange):
    config = exchange.request("GET", "/api/v5/account/config", private=True)
    permissions = set(config[0].get("perm", "").split(",")) if config else set()
    if permissions != {"read_only", "trade"}:
        raise TradingError("模拟盘 API 必须仅有读取和交易权限；无权限信息或含提现/未知权限时停止")
    if config[0].get("posMode") != "net_mode":
        raise TradingError("模拟盘账户不是单向持仓模式，停止联调")
    return {"read": True, "trade": True, "withdraw": False, "position_mode": "net_mode"}


def audit_requests(exchange, summary):
    request = exchange.request
    summary.update({"request_counts": {}, "request_failures": [], "automatic_post_retries": 0})

    def audited(method, path, *args, **kwargs):
        if exchange.settings.mode != "demo":
            raise TradingError("联调只允许明确的模拟盘配置")
        key = method + " " + path
        summary["request_counts"][key] = summary["request_counts"].get(key, 0) + 1
        try:
            return request(method, path, *args, **kwargs)
        except Exception as exc:
            summary["request_failures"].append(
                {
                    "operation": key,
                    "code": exc.code if isinstance(exc, TradingError) else None,
                    "time": datetime.now(UTC).isoformat(),
                }
            )
            raise

    exchange.request = audited


def minimum_order_plan(exchange, settings, symbol, info):
    ct, lot, minimum = (number(info[key]) for key in ("ctVal", "lotSz", "minSz"))
    if min(ct, lot, minimum) <= 0:
        raise TradingError("合约面值、步长或最小张数无效")
    quantity = quantize(minimum, lot, ROUND_CEILING)
    quote = exchange.ticker(symbol)
    price = quantize(number(quote["askPx"]) * (1 + settings.slippage_bps / 10000), number(info["tickSz"]))
    if price <= 0 or quantity * ct * price > 25:
        raise TradingError("最小可成交张数的名义金额超过 25 USDT，停止联调")
    # A half-lot quote cushion allows normal price movement while still rounding
    # down to the minimum. The final guard rejects any other quantity before POST.
    budget = min(Decimal("25"), (quantity + lot / 2) * ct * price)
    place = exchange.place

    def guarded(body):
        size, limit = number(body.get("sz")), number(body.get("px"))
        if body.get("instId") != symbol or body.get("ordType") != "ioc" or size <= 0 or limit <= 0:
            raise ExchangeRejected("受限联调订单参数不符，未 POST")
        if size * ct * limit > 25 or (body.get("side") == "buy" and size != quantity):
            raise ExchangeRejected("受限联调只允许最小开仓张数及单笔不超过 25 USDT，未 POST")
        if body.get("side") == "sell" and (not body.get("reduceOnly") or size > quantity):
            raise ExchangeRejected("受限清理只允许 reduceOnly 且不超过本次最小开仓量，未 POST")
        if body.get("side") not in {"buy", "sell"}:
            raise ExchangeRejected("联调订单方向无效，未 POST")
        return place(body)

    exchange.place = guarded
    return quantity, budget


def account_snapshot(account):
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "equity_usdt": account["equity"],
        "available_usdt": account["available"],
        "unrealized_pnl_usdt": account["unrealized_pnl"],
        "nonzero_positions": sum(number(p.get("pos") or "0") != 0 for p in account["positions"]),
        "pending_count": len(account["pending"]),
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
                previous = db.execute("SELECT state FROM orders WHERE id=?", (client_id,)).fetchone()
                db.execute(
                    "UPDATE orders SET state=?,filled=?,updated=? WHERE id=?",
                    (state, str(number(data.get("accFillSz") or "0")), time.time(), client_id),
                )
                if previous:
                    store.audit(
                        db,
                        client_id,
                        previous["state"],
                        state,
                        "受限模拟查单确认终态",
                        evidence={"filled": str(number(data.get("accFillSz") or "0"))},
                    )
            return data
        time.sleep(0.5)
    raise TradingError("订单尚未结束，停止后续订单；需人工核对")


def summarize_order(data):
    return {
        "client_id_masked": mask_id(data.get("clOrdId")),
        "exchange_id_masked": mask_id(data.get("ordId")),
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
    summary["executed"] = maximum_filled > 0
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
                """INSERT INTO orders(id,scope,strategy_id,symbol,side,quantity,price,state,created,updated,intent_version)
                          VALUES(?,?,?,?,'sell',?,?,'reserved',?,?,1)""",
                (client_id, engine.scope, sid, symbol, str(quantity), str(price), now, now),
            )
        with store.locked() as db:
            db.execute(
                "UPDATE orders SET state='submitting',submit_started=? WHERE id=?", (time.time(), client_id)
            )
            store.audit(db, client_id, "reserved", "submitting", "受限模拟清理提交开始")
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
        except ExchangeRejected as exc:
            with store.locked() as db:
                db.execute(
                    "UPDATE orders SET state='rejected',error=?,updated=? WHERE id=?",
                    (str(exc), time.time(), client_id),
                )
                store.audit(
                    db,
                    client_id,
                    "submitting",
                    "rejected",
                    "受限清理明确未被受理",
                    evidence={"code": exc.code},
                )
            raise TradingError("清理订单被明确拒绝，停止并报告剩余仓位", exc.code) from None
        except TradingError:
            # Unknown receipt is resolved only by GET; never resend the same order.
            with store.locked() as db:
                db.execute("UPDATE orders SET state='unknown',updated=? WHERE id=?", (time.time(), client_id))
        closed = confirm(exchange, store, engine.scope, symbol, client_id)
        summary["cleanup_orders"].append(summarize_order(closed))
        time.sleep(0.5)
    account = exchange.account()
    summary["position_after_cleanup"] = str(position(account, symbol))
    summary["account_after_cleanup"] = account_snapshot(account)
    summary["pending_orders_after_cleanup"] = len(account["pending"])
    summary["strategy_paused"] = all(not s["active"] for s in store.strategies())
    if position(account, symbol) != 0 or account["pending"]:
        raise TradingError("清理后仍有持仓或挂单，需人工核对")


def run(args):
    database = Path(args.database).expanduser().resolve()
    args.database = str(database)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Share the application's process lock as well as its UID-bound ledger.
    with Path(str(database) + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "blocked", "error": "主应用或另一个联调正在使用该数据库；请先停止服务"}
        return _run(args)


def _run(args):
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
    audit_requests(exchange, summary)
    sid, entry_id, proven_fill = None, None, Decimal(0)
    try:
        engine = Engine(settings, store, exchange)
        summary["permissions"] = check_permissions(exchange)
        _ = engine.scope  # Bind/verify the shared application ledger before any smoke operation.
        store.stop()
        account = exchange.account()
        summary["account_baseline"] = account_snapshot(account)
        store.event("受限模拟联调账户基线：" + json.dumps(summary["account_baseline"], ensure_ascii=False))
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
                age = (engine.now_ms() - candles[-1].ts - BARS[bar]) / 1000
                if not 0 <= age <= settings.max_signal_age_seconds:
                    continue
                for fast in (2, 4, 8, 12, 24, 36):
                    for slow in (3, 6, 18, 24, 36, 60, 100):
                        if fast >= slow:
                            continue
                        spec = StrategySpec(symbol=symbol, bar=bar, fast=fast, slow=slow, allocation=0.001)
                        signal = target(candles, spec)
                        signal.update(
                            {"age_seconds": age, "max_age_seconds": settings.max_signal_age_seconds}
                        )
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
        minimum_quantity, budget = minimum_order_plan(exchange, settings, spec.symbol, info)
        settings = replace(settings, max_order=budget)
        engine.settings = settings
        summary.update(
            {
                "symbol": spec.symbol,
                "strategy": spec.model_dump(),
                "signal": signal,
                "contract_spec": {k: info[k] for k in ("ctVal", "lotSz", "minSz", "tickSz")},
                "minimum_contracts": str(minimum_quantity),
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
            "validation": backtest(candles, spec, limits=settings),
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
        summary["account_after_entry"] = account_snapshot(exchange.account())
        summary["application_account_after_entry"] = {
            k: v
            for k, v in engine.account().items()
            if k
            in {
                "equity",
                "available",
                "unrealized_pnl",
                "recent_net_realized",
                "recent_fees_signed",
                "source",
            }
        }
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
        if summary.get("permissions"):
            try:
                summary["final_account"] = account_snapshot(exchange.account())
            except TradingError as exc:
                summary.update(
                    {
                        "status": "blocked",
                        "final_account_sync": "failed",
                        "final_account_error_code": exc.code,
                    }
                )
        exchange.close()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument(
        "--execute-demo", action="store_true", help="Actually submit up to 25 USDT to demo only"
    )
    parser.add_argument(
        "--database",
        default=os.getenv("DATABASE_PATH", Settings().database),
        help="Use the main application ledger; set explicitly if its .env overrides DATABASE_PATH",
    )
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

import hashlib
import json
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import BARS
from .market import ExchangeRejected, TradingError, fixture, number
from .strategy import StrategySpec, target, validate_candles

UNRESOLVED = ("reserved", "unknown", "accepted", "live", "partially_filled")
TERMINAL = {"filled", "canceled", "mmp_canceled"}


def quantize(value, step, rounding=ROUND_FLOOR):
    if step <= 0:
        raise TradingError("合约交易单位无效")
    return (value / step).to_integral_value(rounding=rounding) * step


class Engine:
    def __init__(self, settings, store, exchange):
        self.settings, self.store, self.exchange = settings, store, exchange
        identity = hashlib.sha256(settings.api_key.encode()).hexdigest()[:16]
        self.scope = f"{settings.mode}:{settings.source}:{identity}"
        with store.connect() as db:
            db.execute("INSERT OR IGNORE INTO paper(scope,cash) VALUES(?,10000)", (self.scope,))

    def candles(self, symbol, bar):
        candles = (
            fixture(symbol, bar) if self.settings.source == "offline" else self.exchange.candles(symbol, bar)
        )
        validate_candles(candles, bar)
        return candles

    def paper_account(self, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.paper_account(connection)
        state = dict(db.execute("SELECT * FROM paper WHERE scope=?", (self.scope,)).fetchone())
        price = fixture(state["symbol"] or "BTC-USDT-SWAP")[-1].close
        unrealized = state["quantity"] * (price - state["entry"])
        return {
            **state,
            "equity": state["cash"] + state["quantity"] * price,
            "available": state["cash"],
            "unrealized_pnl": unrealized,
            "net_pnl": state["realized"] + unrealized - state["fees"],
            "source": "offline",
            "initial_capital": 10000,
        }

    def account(self):
        if self.settings.source == "offline":
            return self.paper_account()
        account = self.exchange.account()
        fills = self.exchange.request(
            "GET", "/api/v5/trade/fills-history", {"instType": "SWAP", "limit": 100}, private=True
        )
        bills = self.exchange.request(
            "GET", "/api/v5/account/bills", {"instType": "SWAP", "type": "8", "limit": 100}, private=True
        )
        usdt_fills = [f for f in fills if f.get("instId", "").endswith("-USDT-SWAP")]
        realized = sum((number(f.get("fillPnl") or "0") for f in usdt_fills), Decimal(0))
        fees = sum((number(f.get("fee") or "0") for f in usdt_fills if f.get("feeCcy") == "USDT"), Decimal(0))
        funding = sum((number(b.get("balChg") or "0") for b in bills if b.get("ccy") == "USDT"), Decimal(0))
        return {
            **account,
            "source": "okx",
            "recent_realized_pnl": str(realized),
            "recent_fees_signed": str(fees),
            "recent_funding_signed": str(funding),
            "recent_net_realized": str(realized + fees + funding),
            "fills_count": len(usdt_fills),
            "funding_count": len(bills),
            "scope_note": "成交和资金费各取最近最多 100 条（交易所保留期内），窗口不同；非累计收益，不含未实现盈亏。",
        }

    def start(self, strategy_id):
        if self.settings.source == "okx":
            self.exchange.account()  # Credentials and net-mode preflight.
        with self.store.locked() as db:
            row = db.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
            if not row:
                raise TradingError("策略不存在")
            if json.loads(row["report"]).get("scope") != self.scope:
                raise TradingError("策略的数据源或账户已改变，请重新生成并验证策略")
            db.execute("UPDATE strategies SET active=0")
            db.execute("UPDATE strategies SET active=1 WHERE id=?", (strategy_id,))
        self.store.event(
            f"策略 #{strategy_id} 已启动；数据源 {self.settings.source}，环境 {self.settings.mode}"
        )

    def reconcile(self):
        if self.settings.source == "offline":
            return []
        results = []
        # Reserve is durable before POST. A crash or timeout can only be resolved by GET, never resend.
        for order in self.store.orders(self.scope):
            if order["state"] not in UNRESOLVED:
                continue
            data = self.exchange.order(order["symbol"], order["id"])
            state = data.get("state", "unknown")
            if state not in TERMINAL | {"live", "partially_filled"}:
                state = "unknown"
            with self.store.locked() as db:
                db.execute(
                    "UPDATE orders SET state=?,exchange_id=?,filled=?,updated=?,error=NULL WHERE id=?",
                    (
                        state,
                        data.get("ordId"),
                        str(number(data.get("accFillSz") or "0")),
                        time.time(),
                        order["id"],
                    ),
                )
            results.append({"id": order["id"], "state": state})
        return results

    def execute(self, strategy_id):
        self.reconcile()
        strategies = self.store.strategies()
        row = next((r for r in strategies if r["id"] == strategy_id), None)
        if not row or not row["active"]:
            raise TradingError("策略未启动")
        spec = StrategySpec(**row["spec"])
        candles = self.candles(spec.symbol, spec.bar)
        signal = target(candles, spec)
        age = int(time.time() * 1000) - (signal["candle_ts"] + BARS[spec.bar])
        if age < 0 or age > BARS[spec.bar] + 60_000:
            raise TradingError("K 线已过期或时间超前，拒绝执行")
        side = "buy" if signal["target"] == "long" else "sell"
        key = f"{self.scope}:{spec.symbol}:{spec.bar}:{signal['candle_ts']}:{side}"
        client_id = "wb" + hashlib.sha256(key.encode()).hexdigest()[:30]
        with self.store.locked() as db:
            existing = db.execute("SELECT * FROM orders WHERE id=?", (client_id,)).fetchone()
            if existing:
                return {"action": "duplicate", "order": dict(existing), "signal": signal}
            if not db.execute("SELECT active FROM strategies WHERE id=?", (strategy_id,)).fetchone()[0]:
                raise TradingError("策略已暂停")
            unresolved = db.execute(
                "SELECT 1 FROM orders WHERE scope=? AND state IN (?,?,?,?,?) LIMIT 1",
                (self.scope, *UNRESOLVED),
            ).fetchone()
            if unresolved:
                raise TradingError("存在未确认或未结束订单，请先对账")
            if self.settings.source == "offline":
                paper = self.paper_account(db)
                if paper["quantity"] and paper["symbol"] != spec.symbol:
                    raise TradingError("演练账户仍持有另一合约，请先平仓")
                position = number(paper["quantity"])
                equity, available = number(paper["equity"]), number(paper["available"])
                # Execute against the current quote, not an older 4H/1D signal close.
                price = number(fixture(spec.symbol)[-1].close)
                info = {
                    "ctVal": "0.01" if spec.symbol.startswith("BTC") else "0.1",
                    "lotSz": "0.01",
                    "minSz": "0.01",
                    "tickSz": "0.1",
                }
                ct = number(info["ctVal"])
                position /= ct
                total_notional = position * ct * price
            else:
                info = self.exchange.instrument(spec.symbol)
                ticker = self.exchange.ticker(spec.symbol)
                price = number(ticker["askPx"] if side == "buy" else ticker["bidPx"])
                if price <= 0:
                    raise TradingError("盘口价格不可用")
                account = self.exchange.account()
                if account["pending"]:
                    raise TradingError("账户存在挂单；请先处理挂单再执行")
                equity, available = number(account["equity"]), number(account["available"])
                position, total_notional = Decimal(0), Decimal(0)
                for pos in account["positions"]:
                    size = number(pos.get("pos") or "0")
                    if size == 0:
                        continue
                    total_notional += abs(number(pos["notionalUsd"]))
                    if pos["instId"] == spec.symbol:
                        if pos.get("posSide") != "net" or pos.get("mgnMode") != "isolated" or size < 0:
                            raise TradingError("目标合约需为单向逐仓多仓，拒绝接管空仓或全仓")
                        position += size
                ct = number(info["ctVal"])
            if (side == "buy" and position > 0) or (side == "sell" and position == 0):
                return {"action": "hold", "signal": signal}
            slip = self.settings.slippage_bps / 10_000
            limit = quantize(
                price * (1 + slip if side == "buy" else 1 - slip),
                number(info["tickSz"]),
                ROUND_FLOOR if side == "buy" else ROUND_CEILING,
            )
            if limit <= 0 or ct <= 0:
                raise TradingError("合约价格或面值无效")
            if side == "buy":
                cap = min(self.settings.max_position, equity * self.settings.equity_fraction)
                budget = min(
                    self.settings.max_order,
                    equity * number(spec.allocation),
                    cap - total_notional,
                    available / Decimal("1.01"),
                )
                quantity = quantize(budget / (ct * limit), number(info["lotSz"]))
                if equity <= 0 or budget <= 0:
                    raise TradingError("仓位或可用余额达到上限")
            else:
                quantity = quantize(position, number(info["lotSz"]))
            if quantity < number(info["minSz"]) or quantity <= 0:
                raise TradingError("风控预算不足最小合约张数")
            now = time.time()
            db.execute(
                """INSERT INTO orders(id,scope,strategy_id,symbol,side,quantity,price,state,created,updated)
                          VALUES(?,?,?,?,?,?,?,'reserved',?,?)""",
                (client_id, self.scope, strategy_id, spec.symbol, side, str(quantity), str(limit), now, now),
            )
        # Durable intent is committed above, even if the process dies immediately after submission.
        failure, failure_code = None, None
        with self.store.locked() as db:
            active = db.execute("SELECT active FROM strategies WHERE id=?", (strategy_id,)).fetchone()[0]
            state, exchange_id, filled = "canceled_local", None, "0"
            if active:
                if self.settings.source == "offline":
                    base_qty = float(quantity * ct)
                    fill_price = float(limit)
                    trade_fee = base_qty * fill_price * 0.0005
                    paper = self.paper_account(db)
                    cash, base_position, entry = paper["cash"], paper["quantity"], paper["entry"]
                    realized = paper["realized"]
                    if side == "buy":
                        cash -= base_qty * fill_price + trade_fee
                        base_position, entry = base_qty, fill_price
                    else:
                        cash += base_qty * fill_price - trade_fee
                        realized += base_qty * (fill_price - entry)
                        base_position = max(0, base_position - base_qty)
                    db.execute(
                        "UPDATE paper SET cash=?,quantity=?,entry=?,fees=fees+?,realized=?,symbol=? WHERE scope=?",
                        (cash, base_position, entry, trade_fee, realized, spec.symbol, self.scope),
                    )
                    state, exchange_id, filled = "filled", "paper-" + client_id, str(quantity)
                else:
                    body = {
                        "instId": spec.symbol,
                        "tdMode": "isolated",
                        "posSide": "net",
                        "side": side,
                        "ordType": "ioc",
                        "px": str(limit),
                        "sz": str(quantity),
                        "clOrdId": client_id,
                        "reduceOnly": side == "sell",
                    }
                    try:
                        result = self.exchange.place(body)
                        state, exchange_id = "accepted", result["ordId"]
                    except ExchangeRejected as exc:
                        state, failure = "rejected", str(exc)
                        failure_code = exc.code
                    except Exception:
                        state, failure = "unknown", "订单结果不确定；禁止重发，请先对账"
            db.execute(
                "UPDATE orders SET state=?,exchange_id=?,filled=?,error=?,updated=? WHERE id=?",
                (state, exchange_id, filled, failure, time.time(), client_id),
            )
        self.store.event(f"{spec.symbol} {side} {quantity} 张：{state}", "error" if failure else "info")
        if failure:
            raise TradingError(failure, failure_code)
        return {"action": "submitted", "id": client_id, "state": state, "signal": signal}

    def tick(self):
        for spec in self.store.strategies():
            if spec["active"]:
                try:
                    self.execute(spec["id"])
                except Exception as exc:
                    # Do not include raw exception data which may originate from exchange payloads.
                    message = str(exc) if isinstance(exc, TradingError) else "内部执行失败，请检查服务日志"
                    self.store.stop()
                    self.store.event(message + "；策略已暂停，需人工重新启动", "error")

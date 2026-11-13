import hashlib
import json
import threading
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import BARS
from .coordination import account_scope, bind_account
from .market import ExchangeRejected, TradingError, fixture, number
from .risk import opening_budget
from .strategy import StrategySpec, target, validate_candles

UNRESOLVED = ("reserved", "submitting", "unknown", "accepted", "live", "partially_filled")
TERMINAL = {"filled", "canceled", "mmp_canceled"}


def quantize(value, step, rounding=ROUND_FLOOR):
    if step <= 0:
        raise TradingError("合约交易单位无效")
    return (value / step).to_integral_value(rounding=rounding) * step


class Engine:
    def __init__(self, settings, store, exchange):
        self.settings, self.store, self.exchange = settings, store, exchange
        identity = hashlib.sha256(settings.api_key.encode()).hexdigest()[:16]
        self.legacy_scope = f"{settings.mode}:{settings.source}:{identity}"
        self._scope = self.legacy_scope if settings.source == "offline" else None
        self._identity_lock = threading.Lock()
        self.startup_error = None
        with store.connect() as db:
            if settings.source == "offline":
                db.execute("INSERT OR IGNORE INTO paper(scope,cash) VALUES(?,10000)", (self.scope,))

    @property
    def scope(self):
        with self._identity_lock:
            if self._scope is None:
                scope = account_scope(self.settings.mode, self.exchange.identity())
                bind_account(
                    self.settings.account_state_dir,
                    scope,
                    self.store.path,
                    lambda: self.store.migrate_scope(self.legacy_scope, scope, self.verify_legacy_order),
                )
                self._scope = scope
                self.startup_error = None
            return self._scope

    @property
    def view_scope(self):
        return self._scope or self.legacy_scope

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

    def start(self, strategy_id, expected_epoch=None):
        epoch = self.store.begin_start(expected_epoch)
        if self.settings.source == "okx":
            self.exchange.account()  # Credentials and net-mode preflight.
        self.reconcile(recover_unsent=True)
        if self.store.unresolved(self.scope):
            raise TradingError("存在未结束或异常订单；请持续对账至交易所确认终态")
        with self.store.locked() as db:
            if db.execute("SELECT epoch FROM control WHERE id=1").fetchone()[0] != epoch:
                raise TradingError("启动请求已被暂停或另一操作作废，请刷新后重新确认")
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

    def reconcile(self, recover_unsent=False):
        results = []
        # Keep the database lock through each lookup. A submit and reconcile must
        # never change the same intent concurrently; failure is isolated per order.
        for candidate in self.store.unresolved(self.scope):
            with self.store.locked() as db:
                order = dict(db.execute("SELECT * FROM orders WHERE id=?", (candidate["id"],)).fetchone())
                if order["state"] not in UNRESOLVED:
                    continue
                reason, code, count = None, None, 0
                state, exchange_id, filled = order["state"], order["exchange_id"], order["filled"]
                safe_unsent = (
                    order["state"] == "reserved"
                    and order["intent_version"] == 1
                    and order["submit_started"] is None
                )
                if safe_unsent and not recover_unsent:
                    continue
                if self.settings.source == "offline" or safe_unsent:
                    state, reason = "canceled_local", "未提交意图已取消；保留原信号去重，不重发"
                else:
                    try:
                        data = self.exchange.order(order["symbol"], order["id"])
                        state = data.get("state", "unknown")
                        if state not in TERMINAL | {"live", "partially_filled"}:
                            state = "unknown"
                        exchange_id, filled = data.get("ordId"), str(number(data.get("accFillSz") or "0"))
                    except Exception as exc:
                        state = "unknown"
                        code = exc.code if isinstance(exc, TradingError) else None
                        count = order["not_found_count"] + 1 if code == "51603" else 0
                        reason = (
                            "查无订单不能排除迟到成交；保持阻断并持续对账，禁止作废或重发"
                            if code == "51603"
                            else "查单失败，继续停单；请重试对账"
                        )
                db.execute(
                    "UPDATE orders SET state=?,exchange_id=?,filled=?,updated=?,error=?,last_error_code=?,not_found_count=? WHERE id=?",
                    (state, exchange_id, filled, time.time(), reason, code, count, order["id"]),
                )
                if state == "unknown" and db.execute("SELECT 1 FROM strategies WHERE active=1").fetchone():
                    db.execute("UPDATE strategies SET active=0")
                    db.execute("UPDATE control SET epoch=epoch+1 WHERE id=1")
                self.store.audit(
                    db,
                    order["id"],
                    order["state"],
                    state,
                    reason or "查单更新",
                    evidence={"error_code": code, "not_found_count": count, "filled": filled},
                )
            results.append({"id": order["id"], "state": state, "error": reason, "code": code})
        return results

    def abandon(self, client_id, operator, reason, confirmation):
        # Compatibility endpoint: prior clients must fail closed too. Even many
        # not-found responses and an empty account cannot exclude delayed fills.
        self.store.stop()
        raise TradingError("未知或已提交订单禁止人工作废；请持续对账至交易所确认终态，不得重发")

    def verify_legacy_order(self, order, scope):
        try:
            data = self.exchange.order(order["symbol"], order["id"])
        except Exception:
            raise TradingError(
                "旧 Key 记录账户归属无法核实；保留记录并拒绝迁移，请恢复原 Key 或取得可核实订单"
            ) from None
        matches = (
            data.get("clOrdId") == order["id"]
            and data.get("instId") == order["symbol"]
            and data.get("side") == order["side"]
            and data.get("ordId")
            and (not order["exchange_id"] or data["ordId"] == order["exchange_id"])
            and number(data.get("sz") or "0") == number(order["quantity"])
        )
        if not matches or data.get("state") not in TERMINAL | {"live", "partially_filled"}:
            raise TradingError("旧 Key 订单身份不匹配，无法核实账户归属；拒绝迁移")
        if order["state"] in TERMINAL and data["state"] != order["state"]:
            raise TradingError("旧 Key 订单终态不一致；拒绝迁移")
        return {
            "ownership": "order_lookup_authenticated_account",
            "account_scope": scope,
            **{key: data.get(key) for key in ("clOrdId", "ordId", "instId", "side", "sz", "state")},
        }

    def refresh_signal_age(self, signal):
        now = self.now_ms()
        signal.update(
            {
                "age_seconds": (now - signal["close_ts_ms"]) / 1000,
                "executable": signal["close_ts_ms"] <= now <= signal["deadline_ms"],
            }
        )
        return signal["executable"]

    @staticmethod
    def expired(signal):
        return {
            "action": "expired",
            "signal": signal,
            "note": "信号超出原始有效期或时间超前；等待下一根已完成 K 线，不补单",
        }

    def signal(self, spec):
        signal = target(self.candles(spec.symbol, spec.bar), spec)
        age_ms = self.now_ms() - (signal["candle_ts"] + BARS[spec.bar])
        signal.update(
            {
                "close_ts_ms": signal["candle_ts"] + BARS[spec.bar],
                "deadline_ms": signal["candle_ts"]
                + BARS[spec.bar]
                + self.settings.max_signal_age_seconds * 1000,
                "age_seconds": age_ms / 1000,
                "max_age_seconds": self.settings.max_signal_age_seconds,
                "executable": 0 <= age_ms <= self.settings.max_signal_age_seconds * 1000,
            }
        )
        return signal

    def now_ms(self):
        return int(time.time() * 1000)

    def execute(self, strategy_id):
        self.reconcile()
        strategies = self.store.strategies()
        row = next((r for r in strategies if r["id"] == strategy_id), None)
        if not row or not row["active"]:
            raise TradingError("策略未启动")
        spec = StrategySpec(**row["spec"])
        signal = self.signal(spec)
        with self.store.connect() as db:
            db.execute("UPDATE strategies SET last_signal=? WHERE id=?", (json.dumps(signal), strategy_id))
        if not signal["executable"]:
            return self.expired(signal)
        if (signal["candle_ts"] + BARS[spec.bar]) / 1000 <= self.store.legacy_cutoff(self.scope):
            return {"action": "legacy_signal_blocked", "signal": signal, "note": "旧版意图迁移后不重放旧信号"}
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
                "SELECT 1 FROM orders WHERE scope=? AND state IN (?,?,?,?,?,?) LIMIT 1",
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
                budget = opening_budget(
                    self.settings, equity, available, number(spec.allocation), total_notional
                )
                quantity = quantize(budget / (ct * limit), number(info["lotSz"]))
                if equity <= 0 or budget <= 0:
                    raise TradingError("仓位或可用余额达到上限")
            else:
                quantity = quantize(position, number(info["lotSz"]))
            if quantity < number(info["minSz"]) or quantity <= 0:
                raise TradingError("风控预算不足最小合约张数")
            if not self.refresh_signal_age(signal):
                return self.expired(signal)
            now = time.time()
            db.execute(
                """INSERT INTO orders(id,scope,strategy_id,symbol,side,quantity,price,state,created,updated,intent_version)
                          VALUES(?,?,?,?,?,?,?,'reserved',?,?,1)""",
                (client_id, self.scope, strategy_id, spec.symbol, side, str(quantity), str(limit), now, now),
            )
        # The submit-start marker must commit BEFORE POST. A rollback/crash after
        # the request can never make a possibly submitted intent look unsent.
        with self.store.locked() as db:
            intent = db.execute("SELECT state FROM orders WHERE id=?", (client_id,)).fetchone()
            if intent["state"] != "reserved":
                return {"action": "duplicate", "id": client_id, "state": intent["state"], "signal": signal}
            if not self.refresh_signal_age(signal):
                db.execute(
                    "UPDATE orders SET state='canceled_local',error=?,updated=? WHERE id=?",
                    ("提交标记前信号过期，未 POST", time.time(), client_id),
                )
                self.store.audit(db, client_id, "reserved", "canceled_local", "原始 deadline 已过，未提交")
                return self.expired(signal)
            db.execute(
                "UPDATE orders SET state='submitting',submit_started=?,updated=? WHERE id=?",
                (time.time(), time.time(), client_id),
            )
            self.store.audit(db, client_id, "reserved", "submitting", "提交开始标记已持久化")
        failure, failure_code = None, None
        with self.store.locked() as db:
            intent = db.execute("SELECT state FROM orders WHERE id=?", (client_id,)).fetchone()
            if intent["state"] != "submitting":
                return {"action": "duplicate", "id": client_id, "state": intent["state"], "signal": signal}
            active = db.execute("SELECT active FROM strategies WHERE id=?", (strategy_id,)).fetchone()[0]
            state, exchange_id, filled = "canceled_local", None, "0"
            fresh = self.refresh_signal_age(signal)
            if active and fresh:
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
                    fresh = self.refresh_signal_age(signal)
                    if fresh:
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
            self.store.audit(
                db,
                client_id,
                "submitting",
                state,
                failure or ("原始 deadline 已过，未 POST" if not fresh else "提交结果已记录"),
            )
        if not fresh:
            return self.expired(signal)
        self.store.event(f"{spec.symbol} {side} {quantity} 张：{state}", "error" if failure else "info")
        if failure:
            raise TradingError(failure, failure_code)
        return {"action": "submitted", "id": client_id, "state": state, "signal": signal}

    def tick(self):
        strategies = self.store.strategies()
        if not any(spec["active"] for spec in strategies):
            # A paused runner may only GET unresolved orders, never reactivate or
            # release an unknown. An unverified account still requires operator retry.
            if self._scope is not None:
                self.reconcile()
            return
        for spec in strategies:
            if spec["active"]:
                try:
                    self.execute(spec["id"])
                except Exception as exc:
                    # Do not include raw exception data which may originate from exchange payloads.
                    message = str(exc) if isinstance(exc, TradingError) else "内部执行失败，请检查服务日志"
                    self.store.stop()
                    self.store.event(message + "；策略已暂停，需人工重新启动", "error")

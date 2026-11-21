import asyncio
import csv
import io
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import BARS, SYMBOLS, Settings
from .execution import Engine
from .history import POLL_SECONDS, Collector, History
from .market import OKX, TradingError
from .store import Store
from .strategy import BacktestRequest, StrategySpec, backtest, generate, serialize_candles

STATIC = Path(__file__).parent / "static"


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["START_OFFLINE", "START_DEMO", "START_LIVE"]
    control_epoch: int = Field(ge=0)


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=12, max_length=500)
    confirmation: Literal["ABANDON_WITHOUT_RESEND"]


def create_app(settings=None, exchange=None, public_market=None):
    settings = settings or Settings.from_env()
    cache_path = Path(settings.database).resolve().with_name("market-cache.sqlite3")
    if Path(settings.database).resolve() == cache_path:
        raise ValueError("交易账本不能使用行情缓存文件名")
    store = Store(settings.database)
    exchange = exchange or OKX(settings)
    engine = Engine(settings, store, exchange)
    collector = None
    if settings.history_enabled:
        cache = History(
            cache_path,
            settings.history_max_rows,
            settings.history_days,
            settings.history_budget_mb * 1024 * 1024,
            settings.history_max_pairs,
        )
        collector = Collector(cache, public_market, base_url=settings.base_url)
        engine.history = collector
    research_only = collector is not None and settings.source == "okx" and not settings.credentials_ready

    @asynccontextmanager
    async def lifespan(app):
        # One server process per database. A restart always requires fresh activation.
        import fcntl

        lock_file = open(settings.database + ".lock", "a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError("此数据库已有服务进程运行，请只使用一个 worker") from None
        store.stop()
        try:
            if not research_only:
                await asyncio.to_thread(engine.reconcile, True)
        except TradingError as exc:
            engine.startup_error = str(exc)
            store.event("启动对账失败：" + str(exc), "error")
        stop = asyncio.Event()

        async def run():
            while not stop.is_set():
                await asyncio.to_thread(engine.tick)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except TimeoutError:
                    pass

        async def collect():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(collector.poll)
                except (TradingError, sqlite3.Error, OSError):
                    collector.state.clear()
                    logging.getLogger(__name__).warning("行情缓存不可用，采集暂停至下一轮")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
                except TimeoutError:
                    pass

        market_task = asyncio.create_task(collect()) if collector else None
        task = asyncio.create_task(run())
        try:
            yield
        finally:
            store.stop()
            stop.set()
            await task
            if market_task:
                await market_task
                collector.public.close()
            exchange.close()
            lock_file.close()

    app = FastAPI(title="OKX 策略工作台", version="0.1.0", lifespan=lifespan)
    app.state.engine, app.state.store = engine, store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "仅允许同源本机访问"}, status_code=403)
        if (
            request.method == "POST"
            and request.headers.get("content-type", "").split(";")[0] != "application/json"
        ):
            return JSONResponse({"detail": "请求必须使用 application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(TradingError)
    async def trading_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({"detail": "服务内部错误，请检查日志；策略可使用暂停按钮停止"}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": settings.mode, "source": settings.source}

    @app.get("/api/state")
    def state():
        strategies = []
        for r in store.strategies():
            strategies.append(
                {
                    "id": r["id"],
                    "spec": r["spec"],
                    "active": bool(r["active"]),
                    "source": r["report"].get("source"),
                    "last_signal": json.loads(r["last_signal"]) if r["last_signal"] else None,
                    "validation": {
                        k: v for k, v in r["report"]["validation"].items() if k not in {"curve", "trades"}
                    },
                }
            )
        return {
            "history": collector.status() if collector else None,
            "mode": settings.mode,
            "source": settings.source,
            "credentials_ready": settings.credentials_ready,
            "symbols": SYMBOLS,
            "bars": list(BARS),
            "strategies": strategies,
            "orders": store.orders(engine.view_scope),
            "blockers": store.unresolved(engine.view_scope),
            "startup_error": engine.startup_error,
            "live_supported": False,
            "control_epoch": store.control_epoch(),
            "events": store.events(),
            "risk": {
                "max_order_usdt": str(settings.max_order),
                "max_position_usdt": str(settings.max_position),
                "max_equity_fraction": str(settings.equity_fraction),
                "slippage_bps": str(settings.slippage_bps),
                "max_signal_age_seconds": settings.max_signal_age_seconds,
            },
        }

    @app.get("/api/market")
    def market(
        symbol: Literal["BTC-USDT-SWAP", "ETH-USDT-SWAP"] = "BTC-USDT-SWAP",
        bar: Literal["1m", "1H", "4H", "1D"] = "1H",
    ):
        spec = StrategySpec(symbol=symbol, bar=bar)
        if collector:
            rows = collector.history.rows(symbol, bar, confirmed=False, limit=300)
            return {
                "source": "okx_public",
                "candles": [
                    dict(zip(("ts", "open", "high", "low", "close", "volume", "confirmed"), row, strict=True))
                    for row in rows
                ],
            }
        return {
            "source": settings.source,
            "candles": serialize_candles(engine.candles(spec.symbol, spec.bar)),
        }

    @app.post("/api/market/subscriptions")
    def subscription(spec: StrategySpec):
        if not collector:
            raise TradingError("请启用MARKET_HISTORY_ENABLED后重启服务")
        collector.history.subscribe(spec.symbol, spec.bar)
        return collector.status()

    @app.post("/api/market/unsubscribe")
    def unsubscribe(spec: StrategySpec):
        if not collector:
            raise TradingError("行情采集未启用")
        collector.history.unsubscribe(spec.symbol, spec.bar)
        return collector.status()

    @app.get("/api/account")
    def account():
        if research_only:
            return {"source": "okx_public"}
        return engine.account()

    @app.post("/api/strategies/generate")
    def generate_strategy(spec: StrategySpec):
        report = generate(engine.candles(spec.symbol, spec.bar, minimum=130), spec, settings)
        scope = None if research_only else engine.scope
        report.update({"source": settings.source, "scope": scope})
        report["id"] = store.add_strategy(report)
        store.event(f"已生成策略 #{report['id']}；未启动交易")
        return report

    @app.post("/api/backtests")
    def run_backtest(spec: BacktestRequest):
        result = backtest(
            engine.candles(spec.symbol, spec.bar, minimum=max(10, spec.slow + 2)),
            spec,
            spec.initial_capital,
            spec.fee_bps,
            spec.slippage_bps,
            limits=settings,
        )
        result["source"] = settings.source
        result["id"] = store.save_backtest(spec.model_dump(), result)
        return result

    @app.get("/api/backtests/{backtest_id}/export.csv")
    def export(backtest_id: int):
        with store.connect() as db:
            row = db.execute("SELECT result FROM backtests WHERE id=?", (backtest_id,)).fetchone()
        if not row:
            return JSONResponse({"detail": "回测不存在"}, status_code=404)
        output = io.StringIO()
        result = json.loads(row["result"])
        snapshot = result.get("snapshot", {})
        metadata = {
            "backtest_id": backtest_id,
            **snapshot,
            **{f"risk_{k}": v for k, v in result.get("risk", {}).items()},
        }
        writer = csv.DictWriter(output, fieldnames=["ts", "equity", *metadata])
        writer.writeheader()
        writer.writerows({**point, **metadata} for point in result["curve"])
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="backtest-{backtest_id}.csv"'},
        )

    @app.post("/api/strategies/{strategy_id}/start")
    def start(strategy_id: int, body: StartRequest):
        if research_only:
            raise TradingError("当前仅使用公共行情研究，未配置模拟盘交易凭据")
        expected = "START_OFFLINE" if settings.source == "offline" else "START_" + settings.mode.upper()
        if body.confirmation != expected:
            raise TradingError("启动确认与当前交易环境不符")
        engine.start(strategy_id, body.control_epoch)
        return {"active": strategy_id, "mode": settings.mode, "source": settings.source}

    @app.post("/api/strategies/{strategy_id}/tick")
    def tick(strategy_id: int):
        if research_only:
            raise TradingError("当前仅使用公共行情研究，无法执行订单")
        try:
            return engine.execute(strategy_id)
        except Exception:
            store.stop()
            raise

    @app.post("/api/stop")
    def stop():
        store.stop()
        store.event("已暂停全部策略；已有仓位与交易所订单请在 OKX 处理")
        return {
            "stopped": True,
            "control_epoch": store.control_epoch(),
            "note": "仅停止后续信号执行，未平仓或撤单",
        }

    @app.get("/api/strategies/{strategy_id}/signal")
    def preview_signal(strategy_id: int):
        row = next((s for s in store.strategies() if s["id"] == strategy_id), None)
        if not row:
            raise TradingError("策略不存在")
        return engine.signal(StrategySpec(**row["spec"]))

    @app.post("/api/orders/reconcile")
    def reconcile():
        store.stop()
        return {"orders": engine.reconcile(recover_unsent=True), "strategy_paused": True}

    @app.post("/api/orders/{client_id}/abandon")
    def abandon(client_id: str, body: RecoveryRequest):
        return engine.abandon(client_id, body.operator, body.reason, body.confirmation)

    @app.get("/api/orders/{client_id}/audit")
    def audit(client_id: str):
        with store.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM orders WHERE id=? AND scope=?", (client_id, engine.view_scope)
            ).fetchone()
        if not exists:
            return JSONResponse({"detail": "订单不存在"}, status_code=404)
        return {"audit": store.audit_history(client_id)}

    @app.post("/api/orders/{client_id}/cancel")
    def cancel(client_id: str):
        order = next((r for r in store.orders(engine.scope) if r["id"] == client_id), None)
        if not order:
            raise TradingError("订单不存在")
        if order["state"] not in {"accepted", "live", "partially_filled"} or settings.source != "okx":
            raise TradingError("仅可撤销已确认的未结束 OKX 订单")
        store.stop()
        exchange.cancel(order["symbol"], client_id)
        return {"orders": engine.reconcile()}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()

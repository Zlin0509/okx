import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import BARS, SYMBOLS, Settings
from .execution import Engine
from .market import OKX, TradingError
from .store import Store
from .strategy import BacktestRequest, StrategySpec, backtest, generate, serialize_candles

STATIC = Path(__file__).parent / "static"


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["START_OFFLINE", "START_DEMO", "START_LIVE"]


def create_app(settings=None, exchange=None):
    settings = settings or Settings.from_env()
    store = Store(settings.database)
    exchange = exchange or OKX(settings)
    engine = Engine(settings, store, exchange)

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
        stop = asyncio.Event()

        async def run():
            while not stop.is_set():
                await asyncio.to_thread(engine.tick)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except TimeoutError:
                    pass

        task = asyncio.create_task(run())
        try:
            yield
        finally:
            store.stop()
            stop.set()
            await task
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
                    "validation": {
                        k: v for k, v in r["report"]["validation"].items() if k not in {"curve", "trades"}
                    },
                }
            )
        return {
            "mode": settings.mode,
            "source": settings.source,
            "credentials_ready": settings.credentials_ready,
            "symbols": SYMBOLS,
            "bars": list(BARS),
            "strategies": strategies,
            "orders": store.orders(engine.scope),
            "events": store.events(),
            "risk": {
                "max_order_usdt": str(settings.max_order),
                "max_position_usdt": str(settings.max_position),
                "max_equity_fraction": str(settings.equity_fraction),
                "slippage_bps": str(settings.slippage_bps),
            },
        }

    @app.get("/api/market")
    def market(
        symbol: Literal["BTC-USDT-SWAP", "ETH-USDT-SWAP"] = "BTC-USDT-SWAP",
        bar: Literal["1H", "4H", "1D"] = "1H",
    ):
        spec = StrategySpec(symbol=symbol, bar=bar)
        return {
            "source": settings.source,
            "candles": serialize_candles(engine.candles(spec.symbol, spec.bar)),
        }

    @app.get("/api/account")
    def account():
        return engine.account()

    @app.post("/api/strategies/generate")
    def generate_strategy(spec: StrategySpec):
        report = generate(engine.candles(spec.symbol, spec.bar), spec)
        report.update({"source": settings.source, "scope": engine.scope})
        report["id"] = store.add_strategy(report)
        store.event(f"已生成策略 #{report['id']}；未启动交易")
        return report

    @app.post("/api/backtests")
    def run_backtest(spec: BacktestRequest):
        result = backtest(
            engine.candles(spec.symbol, spec.bar), spec, spec.initial_capital, spec.fee_bps, spec.slippage_bps
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
        writer = csv.DictWriter(output, fieldnames=["ts", "equity"])
        writer.writeheader()
        writer.writerows(json.loads(row["result"])["curve"])
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="backtest-{backtest_id}.csv"'},
        )

    @app.post("/api/strategies/{strategy_id}/start")
    def start(strategy_id: int, body: StartRequest):
        expected = "START_OFFLINE" if settings.source == "offline" else "START_" + settings.mode.upper()
        if body.confirmation != expected:
            raise TradingError("启动确认与当前交易环境不符")
        engine.start(strategy_id)
        return {"active": strategy_id, "mode": settings.mode, "source": settings.source}

    @app.post("/api/strategies/{strategy_id}/tick")
    def tick(strategy_id: int):
        try:
            return engine.execute(strategy_id)
        except Exception:
            store.stop()
            raise

    @app.post("/api/stop")
    def stop():
        store.stop()
        store.event("已暂停全部策略；已有仓位与交易所订单请在 OKX 处理")
        return {"stopped": True, "note": "仅停止后续信号执行，未平仓或撤单"}

    @app.post("/api/orders/reconcile")
    def reconcile():
        return {"orders": engine.reconcile()}

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

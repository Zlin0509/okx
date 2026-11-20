"""Desktop adapter: explicit offline/demo selection, no live mode or web credential API."""

import argparse
import asyncio
import fcntl
import json
import os
import secrets
import socket
import stat
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path


def load_demo_credentials(root):
    path = root / "demo-credentials.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValueError("请先通过原生窗口配置模拟盘凭据") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("模拟盘配置必须是当前用户独占权限的普通文件")
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or any(
            not isinstance(data.get(key), str) or not data[key].strip()
            for key in ("api_key", "secret_key", "passphrase")
        ):
            raise ValueError()
    except (ValueError, OSError):
        raise ValueError("模拟盘配置字段无效，请在原生窗口重新保存") from None
    return data


def configure_environment(root, source):
    from okx_workbench.config import Settings

    if source not in {"offline", "okx"}:
        raise ValueError("桌面版仅支持离线或 OKX 模拟盘")
    for name in list(os.environ):
        if name.startswith("OKX_") or name.startswith("MAX_") or name == "DATABASE_PATH":
            del os.environ[name]
    os.environ.update(
        OKX_MODE="demo",
        OKX_DATA_SOURCE=source,
        PYTHON_DOTENV_DISABLED="1",
        DATABASE_PATH=str(root / "workbench.sqlite3"),
        MAX_ORDER_USDT="25",
        MAX_POSITION_USDT="25",
    )
    if source == "okx":
        data = load_demo_credentials(root)
        return replace(
            Settings.from_env(),
            api_key=data["api_key"],
            secret=data["secret_key"],
            passphrase=data["passphrase"],
        )
    return Settings.from_env()


def guard_demo_start(engine):
    start = engine.start

    def checked(*args, **kwargs):
        if engine.settings.source == "okx":
            from okx_workbench.market import TradingError

            try:
                configs = engine.exchange.request("GET", "/api/v5/account/config", private=True)
            except Exception as exc:
                engine.store.stop()
                raise TradingError(
                    "无法确认模拟盘权限，保持暂停", exc.code if isinstance(exc, TradingError) else None
                ) from None
            config = configs[0] if configs and isinstance(configs[0], dict) else {}
            permissions = (
                set(config.get("perm", "").split(",")) if isinstance(config.get("perm"), str) else set()
            )
            if permissions != {"read_only", "trade"} or config.get("posMode") != "net_mode":
                engine.store.stop()
                raise TradingError("桌面模拟盘须为读取+交易、无提现权限和单向持仓；保持暂停")
        return start(*args, **kwargs)

    engine.start = checked


def main():
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--source", choices=("offline", "okx"), default="offline")
    parser.add_argument("--self-check-offline", action="store_true")
    args = parser.parse_args()
    if args.self_check_offline:
        if args.source != "offline":
            raise ValueError("受控自检不能用于在线账户")
        return self_check_offline()
    root = args.data_dir or Path.home() / "Library/Application Support/OKXWorkbench"
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    os.chdir(root)
    with (root / "desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2
        settings = configure_environment(root, args.source)
        return serve(root, settings)


def serve(root, settings):
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from okx_workbench.app import create_app

    app = create_app(settings)

    assert app.state.engine.settings.mode == "demo"
    source = app.state.engine.settings.source
    guard_demo_start(app.state.engine)
    token = secrets.token_urlsafe(32)
    ready = root / "desktop.json"
    (root / "preferences.example.json").write_text(
        json.dumps({"mode": "demo", "source": source, "live_supported": False}, indent=2) + "\n"
    )
    original_lifespan = app.router.lifespan_context
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]

    @asynccontextmanager
    async def lifespan(instance):
        async with original_lifespan(instance):
            descriptor = os.open(ready, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"pid": os.getpid(), "url": f"http://127.0.0.1:{port}", "token": token}, stream)
            print(
                f"本机工作台（{source} / demo）：http://127.0.0.1:{port}；退出请按 Ctrl+C 或关闭原生窗口。",
                flush=True,
            )
            parent = os.getppid()

            async def watch_launcher():
                while True:
                    await asyncio.sleep(1)
                    if os.getppid() != parent:
                        server.should_exit = True
                        return

            monitor = asyncio.create_task(watch_launcher()) if getattr(sys, "frozen", False) else None
            try:
                yield
            finally:
                if monitor:
                    monitor.cancel()
                ready.unlink(missing_ok=True)

    app.router.lifespan_context = lifespan

    @app.post("/api/desktop/quit")
    async def quit_desktop(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"detail": "本机退出授权无效"}, status_code=403)
        app.state.store.stop()

        async def stop_after_response():
            await asyncio.sleep(0.1)
            server.should_exit = True

        asyncio.create_task(stop_after_response())
        return {"stopped": True, "exiting": True}

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            http="h11",
            ws="none",
            log_level="warning",
            access_log=False,
        )
    )
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        ready.unlink(missing_ok=True)
    return 0


def self_check_offline():
    """Deterministic packaged-runtime test. No online exchange or credential loading."""
    import tempfile
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from okx_workbench.config import BARS
    from okx_workbench.market import fixture
    from okx_workbench.strategy import StrategySpec, target

    with tempfile.TemporaryDirectory(prefix="okx-offline-check-") as directory:
        root = Path(directory)
        old = Path.cwd()
        os.chdir(root)
        configure_environment(root, "offline")
        try:
            from okx_workbench.app import app

            engine = app.state.engine
            # Select a real deterministic synthetic historical interval and a
            # matching simulated clock; never patch target() or force an order.
            end = 1794528000000
            spec = StrategySpec(fast=2, slow=3, allocation=0.001)
            for _ in range(200):
                candles = fixture(spec.symbol, spec.bar, end=end)
                if target(candles, spec)["target"] == "long":
                    break
                end -= BARS[spec.bar]
            else:
                raise AssertionError("No synthetic long signal")
            with (
                patch.object(engine, "candles", return_value=candles),
                patch.object(engine, "now_ms", return_value=end + 60000),
                patch.object(engine, "tick", return_value=None),
                patch(
                    "okx_workbench.execution.fixture",
                    side_effect=lambda symbol="BTC-USDT-SWAP", bar="1H": fixture(symbol, bar, end=end),
                ),
            ):
                with TestClient(app) as client:
                    sid = client.post("/api/strategies/generate", json=spec.model_dump()).json()["id"]
                    epoch = client.get("/api/state").json()["control_epoch"]
                    assert (
                        client.post(
                            f"/api/strategies/{sid}/start",
                            json={"confirmation": "START_OFFLINE", "control_epoch": epoch},
                        ).status_code
                        == 200
                    )
                    first = client.post(f"/api/strategies/{sid}/tick", json={}).json()
                    second = client.post(f"/api/strategies/{sid}/tick", json={}).json()
                    orders = client.get("/api/state").json()["orders"]
                    assert first["action"] == "submitted" and first["state"] == "filled"
                    assert second["action"] == "duplicate" and len(orders) == 1
                    assert orders[0]["state"] == "filled" and float(orders[0]["filled"]) > 0
                    client.post("/api/stop", json={})
                    assert all(not s["active"] for s in client.get("/api/state").json()["strategies"])
                    print(
                        json.dumps(
                            {
                                "test": "controlled_offline_fresh_signal",
                                "simulated_clock": True,
                                "real_okx": False,
                                "first_action": first["action"],
                                "second_action": second["action"],
                                "orders": len(orders),
                                "filled": orders[0]["filled"],
                                "strategy_paused": True,
                            }
                        )
                    )
        finally:
            os.chdir(old)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

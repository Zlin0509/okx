"""Offline-only desktop adapter. The audited trading engine is unmodified."""

import argparse
import asyncio
import fcntl
import json
import os
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path


def main():
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    root = args.data_dir or Path.home() / "Library/Application Support/OKXWorkbench"
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    os.chdir(root)
    # Ignore inherited exchange settings and dotenv files, including parent folders.
    for name in list(os.environ):
        if name.startswith("OKX_") or name.startswith("MAX_") or name == "DATABASE_PATH":
            del os.environ[name]
    os.environ.update(
        OKX_MODE="demo",
        OKX_DATA_SOURCE="offline",
        PYTHON_DOTENV_DISABLED="1",
        DATABASE_PATH=str(root / "workbench.sqlite3"),
    )
    with (root / "desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2
        return serve(root)


def serve(root):
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from okx_workbench.app import app

    # Production Settings still refuses live unconditionally. The desktop adapter
    # additionally provides no route to exchange credentials or online mode.
    assert app.state.engine.settings.source == "offline"
    assert not app.state.engine.settings.credentials_ready
    token = secrets.token_urlsafe(32)
    ready = root / "desktop.json"
    (root / "preferences.example.json").write_text(
        json.dumps({"mode": "demo", "source": "offline", "live_supported": False}, indent=2) + "\n"
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
            print(f"离线工作台：http://127.0.0.1:{port}；退出请按 Ctrl+C 或关闭原生窗口。", flush=True)
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


if __name__ == "__main__":
    raise SystemExit(main())

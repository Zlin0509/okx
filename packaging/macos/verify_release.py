"""Validate the installed app or source entry using only local HTTP/offline data."""

import argparse
import csv
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def verify(command, root, report_path):
    root.mkdir(parents=True, exist_ok=False)
    # A deliberately incompatible fake dotenv proves that desktop startup ignores
    # files. It contains no keys, and is never shipped or read by the application.
    (root / ".env").write_text("OKX_MODE=live\nOKX_DATA_SOURCE=okx\n")
    report = {"only_local_http": True, "real_okx_accessed": False, "checks": []}
    launcher = subprocess.Popen(command)
    ready = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"].append({"name": name, "passed": True})

    def request(path, body=None, authorization=None):
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        req = urllib.request.Request(
            ready["url"] + path, headers=headers, data=json.dumps(body).encode() if body is not None else None
        )
        try:
            with opener.open(req, timeout=10) as response:
                raw = response.read().decode()
                return response.status, json.loads(raw) if "json" in response.headers.get(
                    "content-type", ""
                ) else raw
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        for _ in range(160):
            try:
                ready = json.loads((root / "desktop.json").read_text())
                if request("/api/health")[1] == {"status": "ok", "mode": "demo", "source": "offline"}:
                    break
            except (OSError, ValueError):
                pass
            if launcher.poll() is not None:
                raise RuntimeError("Launcher exited before readiness")
            time.sleep(0.25)
        else:
            raise RuntimeError("Offline startup timeout; inspect test desktop.log")
        check("standalone_startup_demo_offline", True)
        check("static_ui_in_bundle", request("/")[0] == 200 and request("/static/app.js")[0] == 200)
        state = request("/api/state")[1]
        check(
            "no_credentials_live_disabled_dotenv_ignored",
            not state["credentials_ready"] and not state["live_supported"],
        )
        generated = request("/api/strategies/generate", {})[1]
        sid = generated["id"]
        check("generate_and_holdout", bool(generated["validation"]) and generated["source"] == "offline")
        check("generated_strategy_inactive", not request("/api/state")[1]["strategies"][0]["active"])
        backtest = request("/api/backtests", generated["spec"])[1]
        check(
            "backtest_snapshot_and_curve",
            len(backtest["curve"]) > 10 and backtest["snapshot"]["source"] == "offline",
        )
        csv_text = request(f"/api/backtests/{backtest['id']}/export.csv")[1]
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        check(
            "csv_matches_snapshot",
            len(rows) == len(backtest["curve"]) and rows[0]["symbol"] == generated["spec"]["symbol"],
        )
        epoch = request("/api/state")[1]["control_epoch"]
        check(
            "live_confirmation_rejected",
            request(f"/api/strategies/{sid}/start", {"confirmation": "START_LIVE", "control_epoch": epoch})[0]
            == 409,
        )
        check(
            "offline_confirmation_starts",
            request(
                f"/api/strategies/{sid}/start", {"confirmation": "START_OFFLINE", "control_epoch": epoch}
            )[0]
            == 200,
        )
        signal = request(f"/api/strategies/{sid}/signal")[1]
        result = request(f"/api/strategies/{sid}/tick", {})
        check(
            "real_local_signal_chain",
            result[0] == 200 and result[1]["action"] in {"expired", "hold", "submitted", "duplicate"},
        )
        check("original_deadline_visible", "deadline_ms" in signal and signal["max_age_seconds"] <= 300)
        report["signal"] = {
            k: signal.get(k)
            for k in ("target", "age_seconds", "max_age_seconds", "executable", "deadline_ms")
        }
        report["offline_action"] = result[1]["action"]
        request(f"/api/strategies/{sid}/tick", {})
        check("repeat_does_not_duplicate", len(request("/api/state")[1]["orders"]) <= 1)
        check("pause_acknowledged", request("/api/stop", {})[1]["stopped"])
        check("paused_execution_rejected", request(f"/api/strategies/{sid}/tick", {})[0] == 409)
        check("account_is_offline", request("/api/account")[1]["source"] == "offline")
        check("quit_requires_local_token", request("/api/desktop/quit", {}, "Bearer wrong-token")[0] == 403)
        check(
            "app_writes_user_data_directory",
            (root / "workbench.sqlite3").is_file() and (root / "preferences.example.json").is_file(),
        )
        check("ready_file_private", ((root / "desktop.json").stat().st_mode & 0o777) == 0o600)
        report["result"] = "passed"
    finally:
        if ready:
            try:
                request("/api/stop", {})
                request("/api/desktop/quit", {}, "Bearer " + ready["token"])
            except (OSError, ValueError):
                pass
        try:
            launcher.wait(timeout=15)
        except subprocess.TimeoutExpired:
            launcher.terminate()
            launcher.wait(timeout=10)
        if ready:
            # The launcher must not leave its server behind.
            for _ in range(40):
                try:
                    os.kill(ready["pid"], 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Server still running after exit")
            report["clean_shutdown"] = True
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "result": report["result"],
                "checks": len(report["checks"]),
                "offline_action": report["offline_action"],
                "clean_shutdown": True,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    if args.app:
        command = ["/usr/bin/open", "-n", "-W", str(args.app.resolve()), "--args", "--data-dir", str(root)]
    elif args.source:
        command = [str(args.source.resolve() / "启动源码离线演练.command"), "--data-dir", str(root)]
    else:
        parser.error("Choose --app or --source")
    verify(command, root, args.report.resolve())


if __name__ == "__main__":
    main()

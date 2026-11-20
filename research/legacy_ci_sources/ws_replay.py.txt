"""Offline replay of the one authorized public WS package. No capture, orders or returns."""

import argparse
import copy
import csv
import io
import itertools
import json
import subprocess
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .engine import Rejected, number
from .execution_audit import append, canonical, sha
from .synthetic_guard import GUARD_SCOPE, install
from .ws_package import read_capture
from .ws_view import MarketView, readonly

ROOT = Path(__file__).resolve().parent.parent
FREEZE = ROOT / "research/WS_SOURCE_FREEZE_V2.json"
PROTOCOL = ROOT / "research/WS_REPLAY_PROTOCOL_V1.json"
SOURCE = ROOT / "research/WS_CAPTURE_SOURCE_V1.json"


def normalize(records, spec):
    """Driver owns raw input; public views never retain the input list or future observations."""
    events, stats = [], {}
    receive_high = -1
    raw_clock_regressions = 0
    for line, record in enumerate(records, 1):
        if record.get("source") != spec["source"]:
            raise Rejected("mixed_source", "Only the declared source can enter a replay")
        received = record.get("receive_unix_ns")
        if type(received) is not int or received < 0:
            raise Rejected("invalid_receive_time", str(line))
        regression = received < receive_high
        raw_clock_regressions += regression
        receive_high = max(receive_high, received)
        kind = record.get("kind")
        base = {
            "line": line,
            "source": record["source"],
            "receive_ns": received,
            "receive_utc": record.get("receive_utc"),
            "available_ns": receive_high,
            "available_at_ms": (receive_high + 999_999) // 1_000_000,
            "receive_clock_regression": regression,
            "kind": kind,
        }
        if kind != "message":
            events.append({**base, "payload": copy.deepcopy(record), "row_index": None})
            continue
        payload = json.loads(record["raw_text"])
        if "event" in payload:
            events.append(
                {
                    **base,
                    "kind": "ack" if payload["event"] == "subscribe" else "ws_control",
                    "payload": payload,
                    "row_index": None,
                }
            )
            continue
        arg = payload.get("arg", {})
        symbol, channel = arg.get("instId"), arg.get("channel")
        if symbol not in spec["symbols"] or channel not in spec["channels"] or not payload.get("data"):
            raise Rejected("unexpected_stream", str(arg))
        key = symbol + "/" + channel
        stat = stats.setdefault(
            key,
            {
                "messages": 0,
                "rows": 0,
                "first_receive_ns": received,
                "last_receive_ns": received,
                "first_exchange_ts": None,
                "last_exchange_ts": None,
                "max_receive_gap_ns": 0,
                "exchange_regressions": 0,
                "duplicate_payloads": 0,
                "same_timestamp_revisions": 0,
                "negative_latency_rows": 0,
                "_seen": set(),
                "_last_by_ts": {},
                "_high_ts": -1,
            },
        )
        stat["messages"] += 1
        stat["max_receive_gap_ns"] = max(stat["max_receive_gap_ns"], received - stat["last_receive_ns"])
        stat["last_receive_ns"] = received
        for index, row in enumerate(payload["data"]):
            if row.get("instId", symbol) != symbol:
                raise Rejected("instrument_mismatch", key)
            ts = int(row["ts"])
            if ts < 0:
                raise Rejected("invalid_exchange_time", key)
            if channel in {"mark-price", "tickers"}:
                if number(row["markPx" if channel == "mark-price" else "last"]) <= 0:
                    raise Rejected("invalid_price", key)
            fingerprint = sha(canonical(row))
            duplicate = fingerprint in stat["_seen"]
            revision = ts in stat["_last_by_ts"] and fingerprint != stat["_last_by_ts"][ts]
            out_of_order = ts < stat["_high_ts"]
            stat["rows"] += 1
            stat["duplicate_payloads"] += duplicate
            stat["same_timestamp_revisions"] += revision
            stat["exchange_regressions"] += out_of_order
            stat["negative_latency_rows"] += received < ts * 1_000_000
            stat["first_exchange_ts"] = ts if stat["first_exchange_ts"] is None else stat["first_exchange_ts"]
            stat["last_exchange_ts"] = ts
            stat["_high_ts"] = max(stat["_high_ts"], ts)
            stat["_seen"].add(fingerprint)
            stat["_last_by_ts"][ts] = fingerprint
            events.append(
                {
                    **base,
                    "kind": "data",
                    "row_index": index,
                    "symbol": symbol,
                    "channel": channel,
                    "exchange_ts_ms": ts,
                    "latency_ns": received - ts * 1_000_000,
                    "duplicate_payload": duplicate,
                    "same_timestamp_revision": revision,
                    "exchange_out_of_order": out_of_order,
                    "payload": row,
                }
            )
    if not events:
        raise Rejected("empty_capture", "No input records")
    for stat in stats.values():
        for k in [key for key in stat if key.startswith("_")]:
            del stat[k]
    return events, {"streams": stats, "receive_clock_regressions": raw_clock_regressions}


def replay(events, spec, *, on_frame=None):
    """Process by conservative arrival high-water mark and input order; no future fill/calendars."""
    grid = spec["grid_ns"]
    start = (events[0]["available_ns"] // grid + 1) * grid
    stop = events[-1]["available_ns"]
    state, frames, funding, transitions, disconnects = {}, [], [], [], []
    connected, epoch, cursor, disconnected_at = False, 0, 0, None
    previous_funding = {}
    lifecycle_counts = Counter()

    def consume(event):
        nonlocal connected, epoch, disconnected_at
        kind = event["kind"]
        if kind != "data":
            lifecycle_counts[kind] += 1
        if kind == "connect_attempt":
            epoch += 1
            previous_funding.clear()
            state.clear()
            connected = False
        elif kind == "handshake":
            # A handshake starts a fresh state even if the capture omitted connect_attempt.
            previous_funding.clear()
            state.clear()
            connected = event["payload"].get("http_status") == 101
            if connected and disconnected_at is not None:
                disconnects.append(
                    {"start_available_ns": disconnected_at, "end_available_ns": event["available_ns"]}
                )
                disconnected_at = None
        elif (
            kind in {"disconnect", "server_close", "transport_error", "error", "local_close"}
            or kind == "ws_control"
        ):
            connected = False
            state.clear()
            previous_funding.clear()
            if kind != "local_close" and disconnected_at is None:
                disconnected_at = event["available_ns"]
        elif kind == "data":
            # normalize retains every raw arrival; disconnected data is evidence only.
            if not connected:
                return
            key = event["symbol"] + "/" + event["channel"]
            if event["channel"] == "funding-rate":
                funding.append({**event, "interpretation": "state_snapshot_only", "inferred_cashflow": None})
                old = previous_funding.get(event["symbol"])
                new = event["payload"].get("settState")
                if old is not None and old != new:
                    transitions.append(
                        {
                            "symbol": event["symbol"],
                            "available_ns": event["available_ns"],
                            "from": old,
                            "to": new,
                        }
                    )
                previous_funding[event["symbol"]] = new
            if key not in state or event["exchange_ts_ms"] >= state[key]["exchange_ts_ms"]:
                state[key] = {**event, "epoch": epoch}

    for tick in range(start, stop + 1, grid):
        while cursor < len(events) and events[cursor]["available_ns"] <= tick:
            consume(events[cursor])
            cursor += 1
        components, marks = {}, {}
        mark_times = []
        for symbol in spec["symbols"]:
            for channel in spec["channels"]:
                key = symbol + "/" + channel
                row = state.get(key)
                if row is None:
                    components[key] = {"status": "missing_or_disconnected"}
                    continue
                exchange_age = tick - row["exchange_ts_ms"] * 1_000_000
                receive_age = tick - row["receive_ns"]
                limit = (
                    spec["funding_snapshot_max_age_ns"]
                    if channel == "funding-rate"
                    else spec["price_max_age_ns"]
                )
                fresh = 0 <= exchange_age <= limit and 0 <= receive_age <= limit
                component = {
                    "status": "fresh" if fresh else "stale_or_clock_ahead",
                    "source": row["source"],
                    "exchange_ts_ms": row["exchange_ts_ms"],
                    "receive_ns": row["receive_ns"],
                    "receive_utc": row["receive_utc"],
                    "available_ns": row["available_ns"],
                    "exchange_age_ns": exchange_age,
                    "receive_age_ns": receive_age,
                    "epoch": row["epoch"],
                    "line": row["line"],
                    "payload": row["payload"],
                }
                components[key] = component
                if channel == "mark-price" and fresh and connected:
                    marks[symbol] = row["payload"]["markPx"]
                    mark_times.append(row["exchange_ts_ms"])
        view = MarketView(
            tick,
            spec.get("origin", "real_public_capture"),
            connected,
            readonly(components),
            readonly(marks),
            tuple(s for s in spec["symbols"] if s not in marks),
            max(mark_times) - min(mark_times) if mark_times else None,
        )
        frames.append(view)
        if on_frame is not None:
            on_frame(view)
    # Consume the trailing sub-second records for funding/lifecycle diagnostics too. No invented extra grid tick.
    tail = events[cursor:]
    for e in tail:
        consume(e)
    if disconnected_at is not None:
        disconnects.append({"start_available_ns": disconnected_at, "end_available_ns": None})
    return frames, {
        "funding_snapshots": funding,
        "funding_state_transitions": transitions,
        "inferred_settlement_events": [],
        "funding_cashflow": None,
        "disconnect_windows": disconnects,
        "lifecycle": dict(lifecycle_counts),
        "trailing_records_not_on_grid": len(tail),
        "connected_at_end": connected,
    }


def render(raw, spec, verification):
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    events, quality = normalize(records, spec)
    frames, stream_info = replay(events, spec)
    for key, stat in quality["streams"].items():
        components = [f.components[key] for f in frames]
        stat["alignment_status_counts"] = dict(Counter(c["status"] for c in components))
        stat["max_exchange_age_ns_on_grid"] = max(
            (c["exchange_age_ns"] for c in components if "exchange_age_ns" in c), default=None
        )
        stat["max_receive_age_ns_on_grid"] = max(
            (c["receive_age_ns"] for c in components if "receive_age_ns" in c), default=None
        )
    pair_rows = []
    for left, right in itertools.combinations(spec["symbols"], 2):
        valid = [f.available_ns for f in frames if left in f.fresh_marks and right in f.fresh_marks]
        pair_rows.append(
            {
                "left": left,
                "right": right,
                "fresh_common_grid_points": len(valid),
                "first_available_ns": valid[0] if valid else None,
                "last_available_ns": valid[-1] if valid else None,
            }
        )
    complete = [f for f in frames if not f.missing_or_stale_marks and f.connected]
    quality.update(
        {
            "scope": "authorized short real public capture; data quality only",
            "input": verification,
            "normalized_data_rows": sum(e["kind"] == "data" for e in events),
            "normalized_events": len(events),
            "alignment_grid_points": len(frames),
            "complete_fresh_mark_grid_points": len(complete),
            "max_cross_asset_mark_skew_ms": max((f.exchange_mark_skew_ms or 0 for f in frames), default=None),
            "pairs": pair_rows,
            "history": {
                "complete_daily_observations": 0,
                "required_signal_days": spec["minimum_signal_days"],
                "required_statistics_days": spec["statistical_training_days"],
                "required_listing_age_days": spec["listing_age_days"],
                "point_in_time_eligibility": "unknown",
                "model_permission": "refused",
                "reason": "Short intraday quotes are not complete daily history; real statistical qualification remains disabled",
            },
            "real_net_return": None,
            "annualized_return": None,
            "sharpe": None,
            "sequence_level_completeness": "unknown; gaps are observations, never proof of lossless feed",
            "funding": stream_info,
        }
    )
    lines = [
        "# WS 接入／确定性回放（真实短时数据，非策略收益）",
        "",
        "仅导入已核验的 120 秒生产公共包；不采样、不使用凭据或交易接口。",
        "",
        f"规范化数据行 {quality['normalized_data_rows']}，事件 {len(events)}，流 {len(quality['streams'])}；UTC 整秒 as-of 网格 {len(frames)}，8 币 fresh mark 共同点 {len(complete)}。最大跨币 mark 时间差 {quality['max_cross_asset_mark_skew_ms']} ms。",
        "",
        "| 流 | 消息数 | 最大接收间隔 ns | 重复 payload | 同 ts 修订 | 交易所时间倒退 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, row in sorted(quality["streams"].items()):
        lines.append(
            f"| {key} | {row['messages']} | {row['max_receive_gap_ns']} | {row['duplicate_payloads']} | {row['same_timestamp_revisions']} | {row['exchange_regressions']} |"
        )
    lines += [
        "",
        "价格质量门槛为 2 秒、资金费快照诊断门槛为 120 秒，使用各自 exchange 与 receive 年龄；所有原时间及原文保留。重复和乱序不从原始输出删除；较旧 exchange 时间不覆盖当前状态，同 ts 以后收到的为准。断连清空状态，不跨连接沿用报价。无序列号，不能证明无缺包。",
        "",
        f"资金费快照 {len(stream_info['funding_snapshots'])}，只记录状态及转换，不生成结算事件或账户现金流（funding_cashflow=null）。接收钟倒退 {quality['receive_clock_regressions']}，观察到断连窗口 {len(stream_info['disconnect_windows'])}。",
        "",
        "回放只读视图可生成既有 Observed 接口的 intraday as-of 观察，组件保留各自可得时点；这些不是日线，也没有虚构账户权益或执行价。完整日线 0，56／512 日窗口与 180 日上市／历史币池资格未满足或未知，模型许可明确拒绝。无真实收益、年化、夏普或中期净值。",
        "",
        "导入前复用经完整哈希核验的 peer 只读校验器，另用外部 ZIP／raw SHA-256 锚；原包及旧写文件校验器保留且不执行。peer 对额外未列文件仅报告，列表忽略 .pyc／__pycache__；外层对完整 ZIP 每字节及全部解压成员前后哈希另作约束，不能用额外文件绕过信任锚。随机临时路径不参与结果哈希，UTC／HEAD 放在独立运行日志。",
        "",
        "官方字段：[资金费状态](https://www.okx.com/docs-v5/en/#public-data-websocket-funding-rate-channel)、[mark-price](https://www.okx.com/docs-v5/en/#public-data-websocket-mark-price-channel)，2026-11-14 核对。旧普通会员成本／统计冻结与已知并发 P1、非阻断 vector P2、历史与样本外／发布共享门槛不变。",
        "",
    ]
    out = {
        "normalized.jsonl": "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events),
        "alignment.jsonl": "".join(
            json.dumps(f.serialize(), ensure_ascii=False, sort_keys=True) + "\n" for f in frames
        ),
        "quality.json": json.dumps(quality, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "REPORT.md": "\n".join(lines),
    }
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=list(pair_rows[0]) if pair_rows else ["left", "right"], lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(pair_rows)
    out["pairs.csv"] = stream.getvalue()
    return out


def perform(capture, output=None):
    for path, expected in json.loads(FREEZE.read_text())["sha256"].items():
        if sha((ROOT / path).read_bytes()) != expected:
            raise Rejected("freeze_mismatch", path)
    raw, verified = read_capture(capture, json.loads(SOURCE.read_text()))
    spec = json.loads(PROTOCOL.read_text())
    out = render(raw, spec, verified)
    manifest = {p: {"bytes": len(body.encode()), "sha256": sha(body.encode())} for p, body in out.items()}
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if output is not None:
        target = Path(output)
        if target.exists():
            raise Rejected("frozen_directory_exists", "Keep prior WS replay evidence")
        target.mkdir(parents=True)
        for p, body in {**out, "manifest.json": text}.items():
            (target / p).write_text(body, encoding="utf-8")
    q = json.loads(out["quality.json"])
    return {
        "scope": "short real WS data adaptation only; no strategy/returns",
        "manifest_sha256": sha(text.encode()),
        "data_rows": q["normalized_data_rows"],
        "streams": len(q["streams"]),
        "grid_points": q["alignment_grid_points"],
        "fresh_common_marks": q["complete_fresh_mark_grid_points"],
        "model_permission": "refused",
        "real_net_return": None,
        "input_hashes_unchanged": verified["readonly_check"]["input_hashes_unchanged"]
        and verified["input_archive_unchanged"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="Only the authorized ZIP is accepted")
    parser.add_argument("--output", help="Optional new controlled output directory, never overwritten")
    args = parser.parse_args()
    guard = install()  # Blocks original snapshots and all covered Python network audit entries.
    common = {
        "run_id": str(uuid.uuid4()),
        "started_at_utc": datetime.now(UTC).isoformat(),
        "HEAD": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "argv": sys.argv,
        "python": sys.version.split()[0],
        "source_freeze_sha256": sha(FREEZE.read_bytes()),
        "operation": "authorized-ws-replay",
    }
    log = ROOT / "research/runtime/ws_replay.jsonl"
    append(log, {**common, "event": "start"})
    try:
        result = {
            **perform(args.capture, args.output),
            "synthetic_guard": dict(guard),
            "guard_scope": GUARD_SCOPE,
        }
        if any(guard.values()):
            raise PermissionError("unexpected_original_cache_or_network_access")
    except Exception as exc:
        result = {
            "code": getattr(exc, "code", None),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "synthetic_guard": dict(guard),
        }
        append(
            log,
            {
                **common,
                "event": "finish",
                "status": "failed",
                "finished_at_utc": datetime.now(UTC).isoformat(),
                "result": result,
                "result_sha256": sha(canonical(result)),
            },
        )
        print(json.dumps(result), file=sys.stderr)
        raise SystemExit(3 if isinstance(exc, PermissionError) else 2) from None
    append(
        log,
        {
            **common,
            "event": "finish",
            "status": "passed",
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "result": result,
            "result_sha256": sha(canonical(result)),
        },
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

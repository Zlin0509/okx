"""SYNTHETIC ONLY: fixed-fill Lv.1 fee attribution, never a real-return backtest."""

import argparse
import csv
import io
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from decimal import localcontext
from pathlib import Path

from . import m3
from .engine import BPS, ZERO, Rejected, number, serializable, sign
from .execution_audit import append, canonical, sha
from .frozen_sources import verify_historical_sources
from .synthetic_guard import GUARD_SCOPE, install

ROOT = Path(__file__).resolve().parent.parent
FREEZE = ROOT / "research/FEE_SOURCE_FREEZE_V1.json"
EXPECTED = ROOT / "research/FEE_EXPECTED_V1.json"
RESULTS = ROOT / "research/results/fees_lv1_v1"
TOL = number("1e-20")
LABEL = "仅合成固定成交路径成本重算；不是可核验的真实策略收益"


def same(actual, expected, message):
    if abs(actual - expected) > TOL:
        raise Rejected("fee_reconciliation_failed", message)


def quantities(row):
    return {
        (p["book"], p["symbol"]): number(p["quantity"]) for p in row["positions"] if number(p["quantity"])
    }


def reprice(accounting, market, fee_bps, *, origin=None):
    """Hold actual fills fixed. Validate all synthetic funding; do not infer missing fees/rates."""
    if origin != "synthetic_test" or market.provenance != "synthetic_test":
        raise Rejected("real_fee_research_disabled", "Only explicit synthetic funding and fills accepted")
    with localcontext() as context:
        context.prec = 34
        return _reprice(accounting, market.normalized(), number(fee_bps))


def _reprice(a, market, rate):
    market.validate()  # Explicit missing settlement rate/mark is never zero-filled.
    if not ZERO <= rate < BPS:
        raise Rejected("invalid_cost", "Finite fee rate must be in [0,10000) bps")
    required = {
        "ledger",
        "initial_equity",
        "equity",
        "realized_pnl",
        "unrealized_pnl",
        "fees",
        "funding_cashflow",
        "slippage_diagnostic",
    }
    if not required <= set(a) or any(a[k] is None for k in required):
        raise Rejected("missing_accounting", "Explicit accounting/funding totals required")
    frames = {f.ts: f for f in market.frames}
    fills, funding, breaches = [], [], []
    original_fee = new_fee = reference_cashflow = execution_cashflow = turnover = ZERO
    slip = realized = net_funding = ZERO
    positions = {}
    expected_funding, seen_funding, mark_times = {}, set(), []
    previous_ts = -1
    min_equity = number(a["initial_equity"])
    for i, row in enumerate(a["ledger"]):
        ts, event = row["ts"], row["event"]
        if row["seq"] != i or ts < previous_ts or ts not in frames:
            raise Rejected("invalid_ledger", "Ordered ledger and explicit market frames required")
        previous_ts = ts
        if event == "mark":
            mark_times.append(ts)
            for (book, symbol), qty in positions.items():
                if ts in market.funding_calendar[symbol]:
                    expected_funding[ts, book, symbol] = (
                        market.realized_rates[ts, symbol],
                        frames[ts].marks[symbol],
                        qty,
                    )
        if event in {"fill", "risk_fill"}:
            key = (row["book"], row["symbol"])
            delta, price, reference = (
                number(row[k]) for k in ("executed_delta", "execution_price", "reference_price")
            )
            if not delta or price <= 0 or reference <= 0 or ts <= row["known_at"]:
                raise Rejected("invalid_fill", "Actual nonzero next-time fills and positive prices required")
            if reference != frames[ts].execution.get(key[1]):
                raise Rejected("invalid_fill", "Fill must bind the frozen execution reference")
            old = positions.get(key, ZERO)
            closed = min(abs(old), abs(delta)) if old and sign(old) != sign(delta) else ZERO
            opened = abs(delta) - closed
            notional = abs(delta) * price
            fee = notional * rate / BPS
            original_fee += number(row["fee"])
            new_fee += fee
            turnover += notional
            realized += number(row["realized_delta"])
            adverse = delta * (price - reference)
            if adverse < 0:
                raise Rejected("invalid_slippage", "Frozen adverse-fill diagnostic required")
            same(adverse, number(row["slippage_diagnostic"]), "Per-fill embedded slippage")
            slip += adverse
            reference_cashflow -= delta * reference
            execution_cashflow -= delta * price
            positions[key] = old + delta
            if not positions[key]:
                del positions[key]
            fills.append(
                {
                    "seq": i,
                    "ts": ts,
                    "signal": row["signal"],
                    "event": event,
                    "book": key[0],
                    "symbol": key[1],
                    "known_at": row["known_at"],
                    "signed_filled_base_quantity": delta,
                    "position_before": old,
                    "position_after": old + delta,
                    "opened_or_increased_quantity": opened,
                    "closed_or_reduced_quantity": closed,
                    "execution_price": price,
                    "reference_price": reference,
                    "absolute_fill_notional": notional,
                    "fee_bps": rate,
                    "opening_or_increase_fee": opened * price * rate / BPS,
                    "closing_or_reduction_fee": closed * price * rate / BPS,
                    "fee": fee,
                    "original_fee": number(row["fee"]),
                    "cumulative_repriced_fees": new_fee,
                    "slippage_diagnostic_already_embedded": adverse,
                }
            )
        if quantities(row) != positions:
            raise Rejected("invalid_ledger", "Positions disagree with the actual filled quantity path")
        if event == "funding":
            key = (ts, row["book"], row["symbol"])
            if key not in expected_funding or key in seen_funding:
                raise Rejected("missing_funding", "Unexpected or duplicate settlement")
            expected_rate, mark, qty = expected_funding[key]
            if number(row["rate"]) != expected_rate or number(row["settlement_mark"]) != mark:
                raise Rejected("missing_funding", "Settlement must match explicit synthetic mark and rate")
            flow = -qty * mark * expected_rate
            same(number(row["cash_delta"]), flow, "Signed long/short settlement")
            seen_funding.add(key)
            net_funding += flow
            funding.append(
                {
                    "seq": i,
                    "ts": ts,
                    "book": key[1],
                    "symbol": key[2],
                    "signed_quantity": qty,
                    "rate": expected_rate,
                    "settlement_mark": mark,
                    "cash_delta": flow,
                    "source": "SYNTHETIC daily schedule; not actual OKX funding",
                }
            )
        same(number(row["cumulative_fees"]), original_fee, "Original fee accumulation")
        equity = number(row["equity"]) + original_fee - new_fee
        min_equity = min(min_equity, equity)
        if number(row["gross_notional"]) > equity or any(
            number(v) > equity * number("0.25") for v in row["asset_absolute_notional"].values()
        ):
            breaches.append(
                {
                    "seq": i,
                    "ts": ts,
                    "event": event,
                    "repriced_equity": equity,
                    "gross_notional": row["gross_notional"],
                    "asset_absolute_notional": row["asset_absolute_notional"],
                }
            )
    if mark_times != list(market.frame_calendar) or seen_funding != set(expected_funding):
        raise Rejected("missing_funding", "All frames and held-leg settlements must remain present")
    terminal_value = sum((q * market.frames[-1].marks[s] for (_, s), q in positions.items()), ZERO)
    trading = number(a["realized_pnl"]) + number(a["unrealized_pnl"])
    same(execution_cashflow + terminal_value, trading, "Executed-price trading PnL")
    reference_pnl = reference_cashflow + terminal_value
    same(reference_pnl - slip, trading, "Embedded slippage must not be deducted twice")
    for actual, key in [
        (original_fee, "fees"),
        (realized, "realized_pnl"),
        (net_funding, "funding_cashflow"),
        (slip, "slippage_diagnostic"),
    ]:
        same(actual, number(a[key]), "Source total " + key)
    initial = number(a["initial_equity"])
    if initial <= 0:
        raise Rejected("invalid_capital", "Positive initial capital required")
    net = trading + net_funding - new_fee
    same(initial + trading + net_funding - original_fee, number(a["equity"]), "Original equity")
    summary = {
        "label": LABEL,
        "fee_bps": rate,
        "fill_count": len(fills),
        "absolute_filled_notional": turnover,
        "reference_price_trading_pnl": reference_pnl,
        "executed_price_realized_pnl": realized,
        "terminal_unrealized_pnl": number(a["unrealized_pnl"]),
        "executed_price_trading_pnl_including_slippage": trading,
        "slippage_diagnostic_not_subtracted_again": slip,
        "fees": new_fee,
        "synthetic_net_funding": net_funding,
        "synthetic_net_result": net,
        "synthetic_net_return_pct": net / initial * 100,
        "initial_equity": initial,
        "final_equity": initial + net,
        "fee_delta_from_original": new_fee - original_fee,
        "fee_break_even_bps_given_embedded_slippage": (trading + net_funding) / turnover * BPS
        if turnover
        else None,
        "break_even_reason": "signed fee threshold; negative means loss even at zero fee"
        if turnover
        else "unavailable_no_fills",
        "combined_fee_and_slippage_cash_budget": reference_pnl + net_funding,
        "remaining_cost_capacity": net,
        "terminal_open_legs": len(positions),
        "hypothetical_terminal_close_fee": None,
        "minimum_repriced_equity_on_fixed_path": min_equity,
        "risk_breach_event_count_on_fixed_path": len(breaches),
        "risk_budget": "gross<=1x, per-asset absolute<=25%; all source events rechecked, no new trading permission",
        "path_assumption": "Fee-only attribution: no feedback to fills, quantities, signals or risk exits",
    }
    return serializable(
        {"summary": summary, "fills": fills, "funding": funding, "risk_breach_events": breaches}
    )


def csv_text(rows):
    stream = io.StringIO(newline="")
    fields = sorted({key for row in rows for key in row})
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def render():
    m3.verify_freeze()
    baseline = m3.render()  # Rebuild frozen synthetic inputs; never open prior market/results caches.
    manifest = {
        name: {"bytes": len(body.encode()), "sha256": sha(body.encode())} for name, body in baseline.items()
    }
    expected = json.loads((ROOT / "research/M3_R2_EXPECTED_V2.json").read_text())
    if manifest != expected:
        raise Rejected("artifact_mismatch", "Original synthetic signals/ledger changed")
    runs = json.loads(baseline["synthetic.json"])
    sources = json.loads((ROOT / "research/FEE_SOURCES.json").read_text())
    taker, maker = number(sources["taker_bps"]), number(sources["maker_bps"])
    scenarios = {
        "taker_main": taker,
        "maker_optimistic": maker,
        "taker_double_fee_stress": taker * 2,
        "maker_double_fee_stress": maker * 2,
    }
    cases, fill_rows, funding_rows = {}, [], []
    for case in m3.CASES:
        run = runs[case]
        market, *_ = m3.inputs(case)
        rows = {
            name: reprice(run["accounting"], market, rate, origin="synthetic_test")
            for name, rate in scenarios.items()
        }
        original = number(run["accounting"]["equity"])
        same(
            number(rows["taker_main"]["summary"]["final_equity"]),
            original,
            "Lv1 Taker must match original 5bps ledger",
        )
        cases[case] = {
            "source_run_sha256": sha(canonical(run)),
            "signals_sha256": sha(canonical(run["signals"])),
            "funding_source": "SYNTHETIC alternating signed rates and settlement marks, not real funding",
            "scenarios": rows,
        }
        for scenario, content in rows.items():
            fill_rows.extend({"case": case, "scenario": scenario, **row} for row in content["fills"])
        funding_rows.extend({"case": case, **row} for row in rows["taker_main"]["funding"])
    result = {
        "label": LABEL,
        "synthetic_origin": "synthetic_test",
        "source_assumptions": sources,
        "original_synthetic_manifest_sha256": sha(
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        ),
        "real_results": {
            "status": "unavailable",
            "trading_pnl": None,
            "funding": None,
            "net_result": None,
            "annualized_return": None,
            "sharpe": None,
            "reason": "Missing long historical funding, settlement marks and point-in-time universe; synthetic tests cannot estimate real strategy returns",
        },
        "cases_are_independent_tests_not_a_portfolio": True,
        "cases": cases,
    }
    output = {
        "costs.json": json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "fills.csv": csv_text(fill_rows),
        "funding.csv": csv_text(funding_rows),
    }
    lines = [
        "# 普通会员费率成本重算（仅合成，非真实策略收益）",
        "",
        "当前真实净收益、年化和夏普 unavailable；不得从下面人工案例推断。",
        "",
        "| 合成人工案例 | 执行价交易盈亏（含滑点） | 合成净资金费 | Taker 手续费 | Taker 净结果 | Maker 手续费 | Maker 净结果 | 盈亏平衡手续费 bps |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for case, data in cases.items():
        t, mk = (data["scenarios"][k]["summary"] for k in ("taker_main", "maker_optimistic"))

        def fmt(value):
            return "unavailable" if value is None else f"{number(value):.6f}"

        lines.append(
            "| "
            + " | ".join(
                [
                    case,
                    *(
                        fmt(v)
                        for v in [
                            t["executed_price_trading_pnl_including_slippage"],
                            t["synthetic_net_funding"],
                            t["fees"],
                            t["synthetic_net_result"],
                            mk["fees"],
                            mk["synthetic_net_result"],
                            t["fee_break_even_bps_given_embedded_slippage"],
                        ]
                    ),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "金额为人工 USDT 计价；各案例初始权益 10,000，互为独立行为测试，不可加总成策略组合业绩。期末未平部分按合成标记价计入未实现盈亏；没有假造终端成交或补收未发生的平仓费。",
        "",
        "交易盈亏使用实际执行价，5 bps adverse 滑点已在价格内，不在净结果重复扣；参考价交易盈亏、滑点诊断及期末未实现部分见机器 JSON。净结果=执行价交易盈亏+合成净资金费−逐腿手续费。",
        "",
        "主情景全 Taker 5 bps，Maker 2 bps 为纯费率乐观对照，同一成交路径不保证 Maker 实际可成交；限价单不必然 Maker。另附手续费加倍 10/4 bps 压力情景。多空、共享币种不净额减免；开平、加减仓、反转按实际成交量计费。",
        "",
        "盈亏平衡列已考虑当前嵌入滑点；负值表示零手续费也亏，零成交额为 unavailable。JSON 另列总成本预算和费用后余量。固定路径归因不反馈费用变化至仓位或风控；逐事件重估权益和既定风险限额，超限记录不授予新交易许可。",
        "",
        "资金费全部为原案例明确标注的人工正负日历，不代表 OKX 真实结算；真实未知项均为 null，未零填。当前公开 Lv.1 假设不代表历史逐日或具体账户费率，地区／合约特殊费率、返佣、费用舍入未核验。",
        "",
        "来源：[OKX 官方合约手续费说明]("
        + sources["source"]
        + ")，2026-11-14 核验。仅当前公开费率假设，不读取账户凭据。",
        "",
        "复现：`python3 -m research.fee_scenarios verify`（标准库）。旧冻结、信号、种子、统计参数、qualified 与固定假阳性保持；并发 P1、已知 vector 可读非阻断 P2、真实研究／发布／共享／PR／CI 阻断不解除。",
        "",
    ]
    output["REPORT.md"] = "\n".join(lines)
    return output


def perform(action):
    spec = json.loads(FREEZE.read_text())
    verify_historical_sources(ROOT, spec["sha256"])
    output = render()
    manifest = {
        name: {"bytes": len(body.encode()), "sha256": sha(body.encode())} for name, body in output.items()
    }
    if manifest != json.loads(EXPECTED.read_text()):
        raise Rejected("artifact_mismatch", "Cost results differ from fee scenario freeze")
    output["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if action == "write":
        if RESULTS.exists():
            raise Rejected("frozen_directory_exists", "Keep prior fee evidence; use verify")
        RESULTS.mkdir(parents=True)
        for name, body in output.items():
            (RESULTS / name).write_text(body, encoding="utf-8")
    elif RESULTS.exists():
        if {p.name for p in RESULTS.iterdir()} != set(output) or any(
            (RESULTS / p).read_text() != body for p, body in output.items()
        ):
            raise Rejected("artifact_mismatch", "Captured fee output changed")
    return {
        "scope": LABEL,
        "cases": len(m3.CASES),
        "fee_scenarios": 4,
        "real_results": "unavailable",
        "manifest_sha256": sha(output["manifest.json"].encode()),
        "artifacts_present": RESULTS.exists(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "write", "real"))
    args = parser.parse_args()
    if args.action == "real":
        print("real_fee_research_disabled: no real strategy return is available", file=sys.stderr)
        raise SystemExit(2)
    guard = install()
    common = {
        "run_id": str(uuid.uuid4()),
        "operation": "fees-" + args.action,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "HEAD": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "python": sys.version.split()[0],
        "argv": sys.argv,
        "source_freeze_sha256": sha(FREEZE.read_bytes()),
        "scope": "local fee CLI entries only; not a historical/global run count",
    }
    log = ROOT / "research/runtime/fee_scenarios.jsonl"
    append(log, {**common, "event": "start"})
    try:
        result = {**perform(args.action), "synthetic_guard": dict(guard), "guard_scope": GUARD_SCOPE}
        if any(guard.values()):
            raise PermissionError("synthetic_only_guard_violation")
    except Exception as exc:
        result = {
            "code": getattr(exc, "code", None),
            "error_type": type(exc).__name__,
            "synthetic_guard": dict(guard),
            "guard_scope": GUARD_SCOPE,
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
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
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
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

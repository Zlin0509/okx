"""Chinese Markdown from the same data used by the SVGs."""

from ..engine import ZERO, number
from .charts import LABELS
from .metrics import available, unavailable, utc_time

REASONS = {
    "zero_downside_deviation": "下行偏差为零",
    "zero_variance": "至少一条序列方差为零",
    "no_complete_utc_month": "没有完整 UTC 自然月",
    "reviewed_artifact_missing": "缺少已审查输入文件",
    "reviewed_artifact_hash_mismatch": "输入 SHA-256 不符",
    "non_synthetic_origin_refused": "输入不具备合成来源证明",
    "different_utc_dates": "UTC 日期不一致",
    "different_initial_capital": "初始资本不一致",
    "noncontiguous_eligible_dates": "UTC 日期存在缺口、重复或乱序",
    "comparison_reference_unavailable": "对照组不可用",
    "no_gross_exposure": "没有持仓敞口",
    "no_invested_close": "没有持仓中的日收盘",
    "R3_no_event_ledger_or_per_leg_marks": "R3 交付物没有逐事件账本及腿级标记价",
    "R3_no_event_exposure_ledger": "R3 交付物没有事件敞口账本",
    "R3_no_aligned_single_asset_close_panel": "R3 交付物没有可对齐的单币收盘价面板",
}


def explain(reason):
    return REASONS.get(reason, reason)


def fmt(metric, *, percent=False):
    if metric["status"] != "available":
        return "不可用（" + explain(metric["reason"]) + "）"
    value = number(metric["value"])
    return f"{100 * value:.4f}%" if percent else f"{value:.4f}"


def _turnover(row):
    turn = row["turnover"]
    if turn["status"] != "available":
        return fmt(turn)
    return fmt({"status": "available", "value": turn["value"]["over_initial_capital"]}, percent=True)


def _stats(row):
    if row["status"] != "available":
        return [fmt(row)] * 5
    m = row["metrics"]
    return [
        fmt(m["close_total_return"], percent=True),
        fmt(m["close_max_drawdown"], percent=True),
        fmt(m["sortino_365"]),
        fmt(m["monthly_win_rate"], percent=True),
        _turnover(row),
    ]


def markdown(result):
    lines = [
        "# ZLIN-11 组合研究报告 · 冻结合成输入",
        "",
        "**离线合成诊断。真实收益与样本外验收不可用。**",
        "",
        "复用已独立审查的 R2 六配置账本和 R3 验收样例。两组为不同模拟实验，日期不对齐，",
        "跨组收益、相关性及基准比较均不可用。真实历史币池、合格日期及资金费结算证明仍待补齐。",
        "",
        "## R2 六配置与成本敏感性",
        "",
        "| 配置 | 成本情景 | 收盘累计收益 | 收盘最大回撤 | Sortino | 月度胜率 | 换手 / 初始资本 | 同情景减 primary |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    r2 = result["R2"]
    for name, case in r2["runs"].items():
        for scenario, row in case.items():
            diff = r2["sensitivity"][name][f"{scenario}_minus_primary_close_return"]
            lines.append("| " + " | ".join([name, scenario, *_stats(row), fmt(diff, percent=True)]) + " |")
    lines.extend(
        [
            "",
            "| 配置 | 冻结信号 | 参数窗口 | 入场 z | 压力减基础收盘收益 |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for name, row in r2["sensitivity"].items():
        config = row["configuration"]
        lines.append(
            "| "
            + " | ".join(
                [
                    name,
                    config["mode"],
                    str(config["window"]),
                    str(config["entry_z"]),
                    fmt(row["stress_minus_base_close_return"], percent=True),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "基础与压力分别采用冻结的 5/5 bps 和 10/10 bps 费率／滑点。日度统计使用完整 UTC "
            "日收盘，逐笔换手覆盖账本内全部实际成交，包括终端开盘成交。"
            "比较使用同一 UTC 日期和初始资本。",
            "",
            "![六配置日收盘净值](net-value.svg)",
            "",
            "## R2 primary 基础成本的敞口与回撤归因",
            "",
        ]
    )
    no_fills = []
    for name, cases in r2["runs"].items():
        run = cases["base"]
        if run["status"] == "available" and not run["turnover"]["value"]["fill_count"]:
            tally = {
                v["reason"]: v["count"]
                for v in run["signal_reasons"]
                if v["action"] in ("rejected", "no_entry")
            }
            no_fills.append(
                f"{name}（资格或池外拒绝 {tally.get('unqualified_or_outside_point_in_time_pool', 0)} 次，"
                f"偏离未到入场带 {tally.get('deviation_outside_entry', 0)} 次）"
            )
    if no_fills:
        first_section = lines.index("![六配置日收盘净值](net-value.svg)")
        lines[first_section:first_section] = ["基础情景无实际成交：" + "、".join(no_fills) + "。", ""]
    primary = r2["runs"]["primary"]["base"]
    if primary["status"] != "available":
        lines += [fmt(primary), ""]
    else:
        lines += [
            f"合成日历 {primary['first_utc_date']} 至 {primary['last_utc_date']}，"
            f"共 {len(primary['path']['days'])} 个 UTC 日收盘。完整月 "
            + ("、".join(primary["metrics"]["full_month_returns"]) or "无")
            + "。排除边界不完整月 "
            + ("、".join(primary["metrics"]["excluded_partial_months"]) or "无")
            + "。",
            "",
        ]
        concentration = primary["concentration"]
        if concentration["status"] == "available":
            x = concentration["value"]
            largest = max(
                ((number(v), s) for s, v in x["max_asset_over_equity"].items()), default=(ZERO, "无")
            )
            lines += [
                f"事件峰值总敞口 / 权益为 {100 * number(x['max_gross_over_equity']):.4f}%，"
                f"最高单币为 {largest[1]} 的 {100 * largest[0]:.4f}%。"
                f"币种峰值 HHI 为 {fmt(x['max_asset_gross_hhi'])}，关系簿峰值 HHI 为 "
                f"{fmt(x['max_book_gross_hhi'])}，持仓日收盘平均币种 HHI 为 "
                f"{fmt(x['mean_invested_daily_close_asset_hhi'])}。",
                "",
            ]
        else:
            lines += [fmt(concentration), ""]
        atr = primary["drawdown_attribution"]
        if atr["status"] == "available":
            trace = atr["value"]["maximum_event_drawdown"]
            lines.extend(
                [
                    f"最大事件回撤 {100 * number(trace['ratio']):.4f}%，"
                    f"从 {utc_time(trace['peak']['ts'])} 的事件 {trace['peak']['seq']} 到 "
                    f"{utc_time(trace['trough']['ts'])} 的事件 {trace['trough']['seq']}，净变动 "
                    f"{number(trace['net_usdt']):+.8f} USDT。",
                    "",
                    "| 项目 | 对权益变动的贡献（USDT） |",
                    "|---|---:|",
                ]
            )
            for key, label in LABELS.items():
                lines.append(f"| {label} | {number(trace['components_usdt'][key]):+.8f} |")
            lines += ["", "机器结果另列按币种、关系簿、逐腿、逐事件的归因和残差。", ""]
        else:
            lines += [fmt(atr), ""]
        items = primary["correlations"].get("asset_to_portfolio")
        if items:
            lines.extend(["| 单币 | 与组合 Pearson |", "|---|---:|"])
            lines.extend(f"| {key} | {fmt(value)} |" for key, value in sorted(items.items()))
            lines += ["", "机器结果另含币种两两相关矩阵。", ""]
        elif primary["correlations"]["status"] != "available":
            lines += [fmt(primary["correlations"]), ""]
    lines.extend(
        [
            "![最大事件回撤归因](drawdown.svg)",
            "",
            "![单币与组合相关性](correlations.svg)",
            "",
            "## R3 独立 365 日验收样例",
            "",
            "| 成本情景 | 路径 | 收盘累计收益 | 收盘最大回撤 | Sortino | 月度胜率 | 换手 / 初始资本 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for scenario, case in result["R3"].items():
        if case["status"] != "available":
            lines.append("| " + " | ".join([scenario, "全部", *_stats(case)]) + " |")
        else:
            for key, label in (("strategy", "策略"), ("btc", "BTC 现货"), ("equal_weight", "时点等权")):
                lines.append("| " + " | ".join([scenario, label, *_stats(case[key])]) + " |")
    lines.extend(
        [
            "",
            "| 成本情景 | 路径 | CAGR | 日夏普（年化） | Calmar | 策略减该基准收盘收益 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for scenario, case in result["R3"].items():
        if case["status"] != "available":
            lines.append("| " + " | ".join([scenario, "全部", *([fmt(case)] * 4)]) + " |")
            continue
        for key, label in (("strategy", "策略"), ("btc", "BTC 现货"), ("equal_weight", "时点等权")):
            path = case[key]
            risk = path.get("R3_annualized_risk", path)
            values = [
                fmt(
                    available(risk["value"][name])
                    if risk["value"][name] is not None
                    else unavailable("分母为零或未定义"),
                    percent=name == "cagr",
                )
                if risk["status"] == "available"
                else fmt(risk)
                for name in ("cagr", "sharpe", "calmar")
            ]
            delta = fmt(case["close_return_differences"][key], percent=True) if key != "strategy" else "—"
            lines.append("| " + " | ".join([scenario, label, *values, delta]) + " |")
    lines.extend(
        [
            "",
            "R3 策略换手来自冻结的成交名义额摘要，基准换手从各自现货成交核算。"
            "单币相关性、事件集中度和逐事件回撤归因不可用，原因见机器结果。",
            "",
            "## 口径",
            "",
            "- Sortino 以零为目标收益。日均收益乘 √365，除以全部日收益下行平方和均值的平方根。"
            "无交易日保留，零下行偏差不可用。",
            "- 月度胜率是净收益严格大于零的完整 UTC 自然月占比。净收益为零的完整月计入分母。",
            "- 换手累加全部买卖、开平和加减仓的绝对成交名义额，以初始资本归一。跨关系簿不冲抵。",
            "- Pearson 使用同一组相邻日收盘的单币和组合简单收益。首日缺少前一共同收盘，双方均舍弃首日。",
            "- HHI 用绝对名义额份额的平方和。平仓时 HHI 不可用，事件峰值包含开平腿先后造成的瞬时集中。",
            "- 回撤归因使用峰值事件之后至谷值事件的持仓盯市、参考价差、滑点、手续费与资金费。"
            "滑点分项已包含在成交造成的权益变动内。拒绝用缺失项补零或跨日拼接。",
            "",
            "输入哈希、逐日序列和不可用原因见 [results.json](results.json)。"
            "文件校验见 [MANIFEST.json](MANIFEST.json)。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")

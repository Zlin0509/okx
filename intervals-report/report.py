import json
import math
import re
from datetime import timedelta

from offline_store import SPORTS

MISSING = "无数据"
ZONE_LIMITS = (55, 75, 90, 105)
PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


def _metric(value, digits=1):
    return MISSING if value is None else f"{value:.{digits}f}"


def _hours(seconds):
    return f"{seconds / 3600:.2f} 小时"


def _period_total(measurements, format_total, sum_values=sum):
    values = [value for value in measurements if value is not None]
    if not values:
        return MISSING
    total = sum_values(values)
    coverage = f"{len(values)}/{len(measurements)} 条有数据"
    return f"{format_total(total)}（{coverage}）"


def _load_trends(wellness, start):
    rows = [
        (row["day"], *(_metric(row[key]) for key in ("ctl", "atl", "tsb")))
        for row in wellness
        if row["day"] >= start and any(row[key] is not None for key in ("ctl", "atl", "tsb"))
    ]
    if not rows:
        return MISSING
    lines = ["| 日期 | CTL | ATL | TSB |", "| --- | --- | --- | --- |"]
    lines.extend(f"| {' | '.join(row)} |" for row in rows)
    return "\n".join(lines)


def _ratio(watts, weight):
    if watts is None or weight is None:
        return None
    value = watts / weight
    if not math.isfinite(value):
        raise ArithmeticError("相对功率超出可计算范围")
    return value


def _change(current, baseline, digits):
    if current is None:
        return MISSING
    if baseline is None:
        return "未提供完整基线"
    return f"{current - baseline:+.{digits}f}"


def _fitness_metrics(ftp, weight, args):
    values = {}
    for key, current, baseline, digits in (
        ("ftp", ftp, args.baseline_ftp_w, 1),
        ("weight", weight, args.baseline_weight_kg, 1),
        ("w_per_kg", _ratio(ftp, weight), _ratio(args.baseline_ftp_w, args.baseline_weight_kg), 2),
    ):
        values[f"current_{key}"] = _metric(current, digits)
        values[f"baseline_{key}"] = "未提供" if baseline is None else _metric(baseline, digits)
        values[f"{key}_change"] = _change(current, baseline, digits)
    if args.target_ftp_w is None:
        values["goal_gap"] = "未提供 FTP 目标"
    elif ftp is None:
        values["goal_gap"] = f"目标 {args.target_ftp_w:.1f} W，缺少当前 FTP，无法计算差距"
    else:
        delta = ftp - args.target_ftp_w
        values["goal_gap"] = f"目标 {args.target_ftp_w:.1f} W，当前减目标 {delta:+.1f} W"

    if ftp is None:
        values["progress_summary"] = "缺少 FTP 记录，无法判断变化"
    elif args.baseline_ftp_w is None:
        values["progress_summary"] = f"最新 FTP {ftp:.1f} W，未提供比较基线"
    else:
        values["progress_summary"] = f"最新 FTP {ftp:.1f} W，较所填基线变化 {values['ftp_change']} W"
    return values


def _zone_seconds(power_seconds, ftp):
    zones = [0] * 5
    for watts, seconds in power_seconds:
        index = sum(watts * 100 > ftp * limit for limit in ZONE_LIMITS)
        zones[index] += seconds
    return zones


def _zone_metrics(zones):
    sampled_seconds = sum(zones)
    values = {}
    for i, seconds in enumerate(zones, 1):
        values[f"z{i}_duration"] = f"{seconds} 秒" if sampled_seconds else MISSING
        values[f"z{i}_share"] = f"{seconds / sampled_seconds:.1%}" if sampled_seconds else MISSING
    return values


def _intensity(week, ftp, cycling_selected):
    rides = [row for row in week if row["sport"] == "cycling"]
    histograms = [json.loads(row["power_seconds"]) for row in rides if row["power_seconds"] is not None]
    sampled_seconds = sum(sum(histogram.values()) for histogram in histograms)
    if not cycling_selected:
        summary = "所选运动范围未包含骑行"
    elif ftp is None:
        summary = "缺少当前 FTP，无法划分骑行功率区间"
    elif not sampled_seconds:
        summary = "本周无可用于分区的骑行功率时长"
    else:
        sessions = sum(any(histogram.values()) for histogram in histograms)
        summary = f"本周 {sessions}/{len(rides)} 条骑行有功率样本，共 {sampled_seconds} 秒。"
        if all(row["duration_seconds"] is not None for row in rides):
            duration = sum(row["duration_seconds"] for row in rides)
            if duration:
                summary += f"占已导入骑行时长的 {sampled_seconds / duration:.1%}。"

    samples = ((int(power), seconds) for histogram in histograms for power, seconds in histogram.items())
    zones = (
        _zone_seconds(samples, ftp) if cycling_selected and ftp is not None and sampled_seconds else [0] * 5
    )
    return {
        "zone_coverage": summary,
        "zone_ftp": _metric(ftp) if cycling_selected else "不适用",
        **_zone_metrics(zones),
    }


def _report_bounds(as_of):
    monday = as_of - timedelta(days=as_of.weekday())
    return monday.isoformat(), as_of.replace(day=1).isoformat(), as_of.isoformat()


def render_report(db, sha256, args, template):
    week_start, month_start, end = _report_bounds(args.as_of)
    first_day = min(week_start, month_start)
    activities = db.execute(
        "SELECT * FROM activities WHERE sha256 = ? AND day BETWEEN ? AND ?",
        (sha256, first_day, end),
    )
    sports = set(args.sport)
    selected = [row for row in activities if row["sport"] in sports]
    week = [row for row in selected if row["day"] >= week_start]
    month = [row for row in selected if row["day"] >= month_start]
    wellness = db.execute(
        "SELECT * FROM wellness WHERE sha256 = ? AND day <= ? ORDER BY day", (sha256, end)
    ).fetchall()
    ftp_row = db.execute(
        "SELECT * FROM ftp_history WHERE sha256 = ? AND day <= ? ORDER BY day DESC LIMIT 1", (sha256, end)
    ).fetchone()
    weight_row = next((row for row in reversed(wellness) if row["weight_kg"] is not None), None)
    ftp = ftp_row["ftp_w"] if ftp_row is not None else None
    weight = weight_row["weight_kg"] if weight_row is not None else None
    ftp_day = ftp_row["day"] if ftp_row is not None else MISSING
    weight_day = weight_row["day"] if weight_row is not None else MISSING
    fitness = _fitness_metrics(ftp, weight, args)
    trends = _load_trends(wellness, first_day)
    weekly_tss = _period_total([row["tss"] for row in week], _metric, math.fsum)
    missing_inputs = []
    if not week:
        missing_inputs.append("本周训练记录")
    if ftp is None:
        missing_inputs.append("当前 FTP")
    if args.target_ftp_w is None:
        missing_inputs.append("FTP 目标")
    if missing_inputs:
        advice_status = "资料不足"
        advice = f"缺少{'、'.join(missing_inputs)}，暂无法给出功率与时长建议。"
    else:
        advice_status = "规则待定"
        advice = "训练建议规则待制定，暂无法给出功率与时长建议。"
    values = {
        "report_period": f"本周 {week_start} 至 {end}，本月 {month_start} 至 {end}",
        "sport_scope": "、".join(label for sport, label in SPORTS.items() if sport in sports),
        "data_coverage": f"本周 {len(week)} 条活动，本月 {len(month)} 条活动（此文件快照）",
        "weekly_load_summary": f"本周 TSS {weekly_tss}" if weekly_tss != MISSING else "本周无 TSS 数据",
        "risk_summary": "缺少负荷趋势数据，风险未评估" if trends == MISSING else "负荷趋势见下表，风险未评估",
        "next_week_summary": advice_status,
        "next_week_recommendations": advice,
        "weekly_duration": _period_total([row["duration_seconds"] for row in week], _hours),
        "monthly_duration": _period_total([row["duration_seconds"] for row in month], _hours),
        "weekly_tss": weekly_tss,
        "monthly_tss": _period_total([row["tss"] for row in month], _metric, math.fsum),
        "load_trends": trends,
        "metric_dates": f"FTP 日期 {ftp_day}，体重日期 {weight_day}",
        **fitness,
        **_intensity(week, ftp, "cycling" in sports),
    }
    if set(PLACEHOLDER.findall(template)) != values.keys():
        raise ValueError("报告模板字段与渲染器不匹配")
    return PLACEHOLDER.sub(lambda match: values[match[1]], template)

"""Deterministic standalone SVG charts driven by the published result data."""

import json
from xml.etree.ElementTree import Element, SubElement, tostring

from ..engine import ONE, number
from .metrics import align, utc_date

COLORS = ("#2563eb", "#16835a", "#9a3412", "#8b5cf6", "#be185d", "#334155")
LABELS = {
    "mark_pnl": "持仓盯市",
    "reference_to_mark_pnl": "参考价与标记价差",
    "execution_slippage": "成交滑点",
    "fees": "手续费",
    "funding": "资金费",
}


def _text(root, x, y, value, **attrs):
    SubElement(root, "text", {"x": str(x), "y": str(y), **attrs}).text = value


def _canvas(title, height):
    root = Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "viewBox": f"0 0 840 {height}",
            "role": "img",
            "aria-label": title,
        },
    )
    SubElement(root, "title").text = title
    SubElement(root, "rect", {"width": "840", "height": str(height), "fill": "#f8fafc"})
    layer = SubElement(
        root, "g", {"font-family": "Noto Sans CJK SC, sans-serif", "font-size": "14", "fill": "#0f172a"}
    )
    _text(layer, 30, 35, title, **{"font-size": "21", "font-weight": "700"})
    _text(layer, 30, 57, "ZLIN-11 · 冻结合成模拟 · 图中数值源自 results.json", fill="#475569")
    return root, layer


def _line(parent, x1, y1, x2, y2, color="#94a3b8"):
    SubElement(parent, "line", {k: str(v) for k, v in dict(x1=x1, y1=y1, x2=x2, y2=y2, stroke=color).items()})


def _empty(layer, reason):
    _text(layer, 30, 105, "不可用 · " + reason)


def nav_chart(results):
    root, layer = _canvas("六配置净值曲线 · 基础成本 · UTC 日收盘", 405)
    runs = results["R2"]["runs"]
    cases = [(name, pair["base"]) for name, pair in runs.items() if pair["base"]["status"] == "available"]
    if not cases:
        _empty(layer, "缺少已审查且可对齐的账本")
        return tostring(root, encoding="utf-8")
    anchor = cases[0][1]["path"]
    cases = [(name, row) for name, row in cases if not align(anchor, row["path"])]
    days = anchor["days"]
    curves = {
        name: [number(v) / number(row["path"]["capital_usdt"]) for v in row["path"]["equity_at_close_usdt"]]
        for name, row in cases
    }
    lo, hi = min(min(x) for x in curves.values()), max(max(x) for x in curves.values())
    padding = max(number("0.0001"), (hi - lo) / 10)
    lo, hi = lo - padding, hi + padding
    left, right, top, bottom = 99, 800, 83, 310
    for i in range(5):
        y = bottom - (bottom - top) * i / 4
        value = lo + (hi - lo) * i / 4
        _line(layer, left, y, right, y, "#cbd5e1")
        _text(layer, left - 8, round(y + 5, 2), f"{value:.4f}", **{"text-anchor": "end"})
    _text(layer, left, bottom + 23, str(utc_date(days[0])))
    _text(layer, right, bottom + 23, str(utc_date(days[-1])), **{"text-anchor": "end"})
    for i, (name, values) in enumerate(curves.items()):
        points = " ".join(
            f"{left + j * (right - left) / max(1, len(values) - 1):.3f},"
            f"{bottom - float((v - lo) / (hi - lo)) * (bottom - top):.3f}"
            for j, v in enumerate(values)
        )
        attrs = {
            "points": points,
            "fill": "none",
            "stroke": COLORS[i],
            "stroke-width": "2",
            "data-field": f"R2.runs.{name}.base.path",
            "data-values": json.dumps([str(v) for v in values], separators=(",", ":")),
            "data-axis": json.dumps([left, right, top, bottom, str(lo), str(hi)], separators=(",", ":")),
        }
        SubElement(layer, "polyline", attrs)
        x = 30 + (i % 3) * 255
        y = 358 + 23 * (i // 3)
        _line(layer, x, y - 4, x + 29, y - 4, COLORS[i])
        _text(layer, x + 36, y, name)
    return tostring(root, encoding="utf-8")


def _bars(title, items, *, max_abs=None, digits=4):
    root, layer = _canvas(title, max(155, 103 + len(items) * 33))
    if not items:
        _empty(layer, "缺少已审查的可计算输入")
        return tostring(root, encoding="utf-8")
    center, extent = 485, 240
    bound = max_abs or max((abs(number(v)) for _, _, v in items if v is not None), default=ONE) or ONE
    _line(layer, center, 76, center, 87 + len(items) * 33)
    for i, (label, field, value) in enumerate(items):
        y = 88 + i * 33
        _text(layer, 30, y + 14, label)
        if value is None:
            _text(layer, center + 15, y + 14, "不可用")
            continue
        numeric = number(value)
        width = extent * float(abs(numeric) / bound)
        attrs = {
            "x": f"{center - width if numeric < 0 else center:.3f}",
            "y": str(y),
            "width": f"{width:.3f}",
            "height": "18",
            "fill": "#b45309" if numeric < 0 else "#167557",
            "data-field": field,
            "data-value": str(value),
            "data-scale-max": str(bound),
        }
        SubElement(layer, "rect", attrs)
        _text(layer, 807, y + 14, f"{numeric:+.{digits}f}", **{"text-anchor": "end"})
    return tostring(root, encoding="utf-8")


def render_charts(results):
    primary = results["R2"]["runs"]["primary"]["base"]
    dd = primary.get("drawdown_attribution", {}).get("value") or {}
    components = dd.get("maximum_event_drawdown", {}).get("components_usdt", {})
    corr = primary.get("correlations", {}).get("asset_to_portfolio", {})
    path = "R2.runs.primary.base"
    return {
        "net-value.svg": nav_chart(results),
        "drawdown.svg": _bars(
            "primary 最大事件回撤归因 · USDT",
            [
                (
                    label,
                    f"{path}.drawdown_attribution.value.maximum_event_drawdown.components_usdt.{key}",
                    components[key],
                )
                for key, label in LABELS.items()
                if key in components
            ],
        ),
        "correlations.svg": _bars(
            "primary 单币与组合相关性 · Pearson",
            [
                (asset, f"{path}.correlations.asset_to_portfolio.{asset}.value", metric.get("value"))
                for asset, metric in sorted(corr.items())
            ],
            max_abs=ONE,
            digits=3,
        ),
    }

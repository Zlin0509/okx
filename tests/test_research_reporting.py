"""Small independent fixtures for reporting and the data-to-figure interface."""

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
from xml.etree.ElementTree import fromstring

import pytest

from research.engine import Costs, Engine, Frame, Leg, Market, Rejected, Signal, number
from research.execution_audit import sha
from research.portfolio import DAY
from research.reporting.attribution import EPS, analyze
from research.reporting.charts import render_charts
from research.reporting.metrics import (
    available,
    correlations,
    daily_metrics,
    daily_path,
    pearson,
    unavailable,
)
from research.reporting.report import _comparison, _r3_path, _read, _synthetic

D = Decimal
SVG = {"s": "http://www.w3.org/2000/svg"}


def days(start, n):
    first = (date.fromisoformat(start) - date(1970, 1, 1)).days * DAY
    return tuple(first + i * DAY for i in range(n))


def test_sortino_all_days_and_zero_downside():
    path = daily_path((0, DAY, 2 * DAY), 100, ["90", "90", "108"])
    sortino = daily_metrics(path)["sortino_365"]["value"]
    assert float(sortino) == pytest.approx(365**0.5 / 3**0.5)
    assert daily_metrics(daily_path((0, DAY), 100, [100, 100]))["sortino_365"] == unavailable(
        "zero_downside_deviation"
    )


def test_monthly_win_rate_only_full_months_including_flat():
    calendar = days("2024-01-31", 63)
    closes = [100] * 30 + [110] * 32 + [120]
    metrics = daily_metrics(daily_path(calendar, 100, closes))
    assert metrics["full_month_returns"] == {"2024-02": 0, "2024-03": D("0.1")}
    assert metrics["monthly_win_rate"] == available(D("0.5"))
    assert metrics["excluded_partial_months"] == {
        "2024-01": {"observed_days": 1, "calendar_days": 31},
        "2024-04": {"observed_days": 2, "calendar_days": 30},
    }


def test_daily_grid_refuses_missing_and_wrong_capital_in_comparison():
    with pytest.raises(Rejected, match="Daily paths cannot compress"):
        daily_path((0, 2 * DAY), 100, [100, 110])

    def run(calendar, capital):
        path = daily_path(calendar, capital, [102, 101])
        return {"status": "available", "path": path, "metrics": daily_metrics(path)}

    actual = run((0, DAY), 100)
    assert _comparison(actual, run((DAY, 2 * DAY), 100)) == unavailable("different_utc_dates")
    assert _comparison(actual, run((0, DAY), 500)) == unavailable("different_initial_capital")
    assert _comparison(actual, unavailable("missing")) == unavailable("comparison_reference_unavailable")


def test_correlation_binds_interval_dates_and_excludes_zero_variance():
    calendar = days("2024-01-01", 3)
    path = daily_path(calendar, 100, [70, 77, "69.3"])
    marks = {
        t + DAY - 1: {"match": a, "constant": 1} for t, a in zip(calendar, (70, 77, "69.3"), strict=True)
    }
    result = correlations(path, marks)
    assert result["paired_days"] == calendar[1:]
    assert result["asset_to_portfolio"]["match"] == available(1)
    assert result["asset_to_portfolio"]["constant"] == unavailable("zero_variance")
    del marks[calendar[1] + DAY - 1]
    assert correlations(path, marks) == unavailable("missing_aligned_asset_close")
    assert pearson([D(-1), D(1)], [D(1), D(-1)]) == available(-1)


@pytest.fixture
def executed_ledger():
    prices = {0: 100, DAY - 1: 100, DAY: 100, 2 * DAY - 1: 90, 2 * DAY: 90, 3 * DAY - 1: 80, 3 * DAY: 80}
    opens = (0, DAY, 2 * DAY, 3 * DAY)
    frames = tuple(Frame(t, {"A": p}, {"A": p} if t in opens else {}) for t, p in prices.items())
    funding = {(2 * DAY, "A"): D("0.01")}
    market = Market(
        frames,
        tuple(prices),
        {"A": opens},
        {"A": (2 * DAY,)},
        funding,
        "synthetic_test",
        "mock explicit calendar",
    )
    long, short = Leg("long-book", "A"), Leg("short-book", "A")
    ledger = Engine(market, initial=2000, costs=Costs(10, 20)).run(
        [
            Signal("long", 0, {long: D("2")}),
            Signal("short", 0, {short: D("-1")}),
            Signal("close", 2 * DAY, {long: D(0), short: D(0)}),
        ]
    )
    return ledger, {t: {"A": p} for t, p in prices.items()}


def test_event_bridge_matches_costs_funding_and_both_books(executed_ledger):
    ledger, frames = executed_ledger
    with localcontext() as context:
        context.prec = 34
        stats = analyze(ledger, frames)
        result = stats["attribution"]
        full = result["full_window"]
        parts = full["components_usdt"]
        assert parts == {
            "mark_pnl": D("-20"),
            "reference_to_mark_pnl": 0,
            "execution_slippage": D("-1.08"),
            "fees": D("-0.54004"),
            "funding": D("-0.90"),
        }
        assert sum(x["net_usdt"] for x in full["by_book"].values()) == full["net_usdt"]
        assert full["by_asset"]["A"]["net_usdt"] == full["net_usdt"]
        assert full["net_usdt"] == ledger["equity"] - ledger["initial_equity"]
        dd = result["maximum_event_drawdown"]
        assert dd["trough"]["equity_usdt"] - dd["peak"]["equity_usdt"] == dd["net_usdt"]
        assert max(abs(x["residual_usdt"]) for x in result["events"]) <= EPS
        assert stats["concentration"]["max_asset_gross_hhi"] == available(1)
        assert stats["turnover"]["absolute_fills_usdt"] == D("540.04")
        assert stats["terminal"]["ts"] == 3 * DAY
        assert stats["path"]["equity_at_close_usdt"][-1] != stats["terminal"]["equity_usdt"]


def test_event_bridge_rejects_substituted_mark(executed_ledger):
    ledger, frames = executed_ledger
    frames = deepcopy(frames)
    frames[2 * DAY - 1]["A"] += 1
    with pytest.raises(Rejected, match="event_equity_does_not_reconcile"):
        analyze(ledger, frames)


def test_bridge_checks_recorded_absolute_exposure(executed_ledger):
    ledger, frames = executed_ledger
    ledger = deepcopy(ledger)
    close_with_legs = next(row for row in ledger["ledger"] if row["positions"])
    close_with_legs["asset_absolute_notional"] = {"B": close_with_legs["gross_notional"]}
    with pytest.raises(Rejected, match="gross_exposure_does_not_reconcile_positions"):
        analyze(ledger, frames)


def test_artifact_must_match_anchor_before_parse_and_stay_synthetic(tmp_path):
    artifact = tmp_path / "input.json"
    artifact.write_text('{"origin":"real"}')
    with pytest.raises(Rejected, match="hash_mismatch"):
        _read(artifact, sha(b"reviewed"))
    with pytest.raises(Rejected, match="non_synthetic_origin_refused"):
        _synthetic(_read(artifact, sha(artifact.read_bytes())))
    private = tmp_path / "research" / "snapshots" / "input.json"
    private.parent.mkdir(parents=True)
    private.write_bytes(artifact.read_bytes())
    with pytest.raises(Rejected, match="controlled_artifact_path_refused"):
        _read(private, sha(artifact.read_bytes()))


def test_r3_spot_nav_must_match_returns_and_have_contiguous_dates():
    series = {
        "origin": "synthetic_test",
        "scenario": "base",
        "capital_usdt": 100,
        "days": [0, DAY],
        "values": [0.01, 0],
    }
    assert _r3_path(series, "base", [101, 101])["equity_at_close_usdt"] == (101, 101)
    with pytest.raises(Rejected, match="r3_spot_returns_do_not_reconcile_nav"):
        _r3_path(series, "base", [101, 102])
    with pytest.raises(Rejected, match="Daily paths cannot compress"):
        _r3_path({**series, "days": [0, 2 * DAY]}, "base", [101, 101])


def _lookup(body, path):
    for key in path.split("."):
        body = body[key]
    return body


def test_figures_use_exact_report_fields():
    path = daily_path((0, DAY, 2 * DAY), 100, [100, 101, 99])
    reconciled = {
        "status": "available",
        "path": path,
        "drawdown_attribution": available(
            {
                "maximum_event_drawdown": {
                    "components_usdt": {"mark_pnl": D("-1.74"), "fees": D("-.26")},
                }
            }
        ),
        "correlations": {
            "asset_to_portfolio": {"A": available(D(".5")), "flat": unavailable("zero_variance")}
        },
    }
    # A series with different dates is omitted from the comparison chart.
    different = {"status": "available", "path": daily_path((DAY, 2 * DAY, 3 * DAY), 100, [100, 99, 98])}
    result = {"R2": {"runs": {"primary": {"base": reconciled}, "returns": {"base": different}}}}
    graphics = render_charts(result)
    (nav,) = fromstring(graphics["net-value.svg"]).findall(".//s:polyline", SVG)
    source = _lookup(result, nav.attrib["data-field"])
    values = [v / source["capital_usdt"] for v in source["equity_at_close_usdt"]]
    assert list(map(number, json.loads(nav.attrib["data-values"]))) == values
    left, right, top, bottom, low, high = json.loads(nav.attrib["data-axis"])
    for index, pair in enumerate(nav.attrib["points"].split()):
        x, y = map(float, pair.split(","))
        assert x == pytest.approx(left + (right - left) * index / 2, abs=0.001)
        assert y == pytest.approx(
            bottom - float((values[index] - D(low)) / (D(high) - D(low))) * (bottom - top), abs=0.001
        )
    for chart in ("drawdown.svg", "correlations.svg"):
        bars = [r for r in fromstring(graphics[chart]).findall(".//s:rect", SVG) if "data-field" in r.attrib]
        assert len(bars) == (2 if chart == "drawdown.svg" else 1)
        for style in (b.attrib for b in bars):
            value = _lookup(result, style["data-field"])
            assert number(style["data-value"]) == value
            assert float(style["width"]) == pytest.approx(
                float(abs(value) / D(style["data-scale-max"])) * 240, abs=0.001
            )

"""Daily and calendar metrics with explicit availability and comparison domains."""

import calendar
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from ..engine import ONE, ZERO, Rejected, number
from ..experiments.evaluation_inputs import daily_grid
from ..portfolio import DAY

EPOCH = date(1970, 1, 1)


def unavailable(reason):
    return {"status": "unavailable", "value": None, "reason": reason}


def available(value):
    return {"status": "available", "value": value}


def utc_date(day):
    return EPOCH + timedelta(days=day // DAY)


def utc_time(ts):
    return datetime.fromtimestamp(ts / 1000, UTC).isoformat(timespec="milliseconds")


def require(condition, reason):
    if not condition:
        raise Rejected(reason, reason)


def daily_path(days, initial, closes):
    days = daily_grid(days)
    initial = number(initial)
    closes = tuple(map(number, closes))
    require(initial > 0 and len(closes) == len(days) and all(v > 0 for v in closes), "invalid_daily_path")
    previous = (initial, *closes[:-1])
    return {
        "days": days,
        "capital_usdt": initial,
        "equity_at_close_usdt": closes,
        "daily_returns": tuple(now / prev - ONE for now, prev in zip(closes, previous, strict=True)),
    }


def align(left, right):
    if left["days"] != right["days"]:
        return "different_utc_dates"
    if left["capital_usdt"] != right["capital_usdt"]:
        return "different_initial_capital"
    return None


def daily_metrics(path):
    returns = path["daily_returns"]
    n = len(returns)
    mean = sum(returns, ZERO) / n
    downside = (sum((min(r, ZERO) ** 2 for r in returns), ZERO) / n).sqrt()
    monthly = defaultdict(list)
    closes = path["equity_at_close_usdt"]
    starts = (path["capital_usdt"], *closes[:-1])
    for ts, opening, close in zip(path["days"], starts, closes, strict=True):
        day = utc_date(ts)
        monthly[day.strftime("%Y-%m")].append((day, opening, close))
    completed, partial = {}, {}
    for key, rows in monthly.items():
        start = rows[0][0]
        planned = calendar.monthrange(start.year, start.month)[1]
        if start.day != 1 or len(rows) != planned:
            partial[key] = {"observed_days": len(rows), "calendar_days": planned}
        else:
            completed[key] = rows[-1][2] / rows[0][1] - ONE
    win_months = sum(r > 0 for r in completed.values())
    initial = path["capital_usdt"]
    peak, max_drawdown = initial, ZERO
    for close in path["equity_at_close_usdt"]:
        peak = max(peak, close)
        max_drawdown = max(max_drawdown, ONE - close / peak)
    return {
        "close_total_return": available(path["equity_at_close_usdt"][-1] / initial - ONE),
        "close_max_drawdown": available(max_drawdown),
        "sortino_365": (
            available(mean * number(365).sqrt() / downside)
            if n >= 2 and downside
            else unavailable("insufficient_daily_returns" if n < 2 else "zero_downside_deviation")
        ),
        "monthly_win_rate": (
            available(number(win_months) / len(completed))
            if completed
            else unavailable("no_complete_utc_month")
        ),
        "full_month_returns": completed,
        "excluded_partial_months": partial,
        "winning_full_months": win_months,
    }


def pearson(left, right):
    require(len(left) == len(right), "different_correlation_dates")
    n = len(left)
    if n < 2:
        return unavailable("insufficient_paired_daily_returns")
    x_mean, y_mean = sum(left, ZERO) / n, sum(right, ZERO) / n
    x, y = [v - x_mean for v in left], [v - y_mean for v in right]
    var_x, var_y = sum(v * v for v in x), sum(v * v for v in y)
    if not var_x or not var_y:
        return unavailable("zero_variance")
    covariance = sum((a * b for a, b in zip(x, y, strict=True)), ZERO)
    return available(max(-ONE, min(ONE, covariance / (var_x * var_y).sqrt())))


def correlations(path, close_marks):
    days = path["days"]
    if any(day + DAY - 1 not in close_marks for day in days):
        return unavailable("missing_aligned_asset_close")
    panel = [close_marks[day + DAY - 1] for day in days]
    if not panel[0] or any(set(row) != set(panel[0]) for row in panel):
        return unavailable("incomplete_aligned_asset_panel")
    assets = sorted(panel[0])
    if any(number(row[s]) <= 0 for row in panel for s in assets):
        return unavailable("invalid_asset_close")
    changes = {
        symbol: [number(b[symbol]) / number(a[symbol]) - ONE for a, b in zip(panel, panel[1:], strict=False)]
        for symbol in assets
    }
    portfolio = path["daily_returns"][1:]
    return {
        "status": "available",
        "paired_days": days[1:],
        "sample_count": len(portfolio),
        "asset_to_portfolio": {s: pearson(changes[s], portfolio) for s in assets},
        "asset_to_asset": {a: {b: pearson(changes[a], changes[b]) for b in assets} for a in assets},
    }

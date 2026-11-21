"""Event replay for exact equity bridges and absolute exposure diagnostics."""

from collections import defaultdict

from ..engine import ONE, ZERO, number
from ..portfolio import DAY
from .metrics import available, daily_path, require, unavailable

COMPONENTS = ("mark_pnl", "reference_to_mark_pnl", "execution_slippage", "fees", "funding")
EPS = number("1e-22")


def _positions(row):
    result = {}
    for leg in row["positions"]:
        key = (leg["book"], leg["symbol"])
        qty = number(leg["quantity"])
        require(key not in result, "duplicate_book_leg")
        if qty:
            result[key] = qty
    return result


def _add(totals, parts):
    for key, value in parts.items():
        totals[key] += value


def _components(parts):
    return {c: sum((v for (_, _, kind), v in parts.items() if kind == c), ZERO) for c in COMPONENTS}


def _attributed(parts):
    totals = _components(parts)
    books, assets, legs = {}, {}, {}
    for (book, symbol, kind), value in sorted(parts.items()):
        for rows, key in ((books, book), (assets, symbol), (legs, (book, symbol))):
            rows.setdefault(key, dict.fromkeys(COMPONENTS, ZERO))[kind] += value

    def total(group):
        return {**group, "net_usdt": sum(group.values(), ZERO)}

    return {
        "components_usdt": totals,
        "net_usdt": sum(totals.values(), ZERO),
        "by_book": {key: total(row) for key, row in books.items()},
        "by_asset": {key: total(row) for key, row in assets.items()},
        "by_leg": [{"book": b, "symbol": s, **total(row)} for (b, s), row in legs.items()],
    }


def _concentration(row, positions, marks):
    equity = number(row["equity"])
    gross = number(row["gross_notional"])
    asset = {s: number(v) for s, v in row["asset_absolute_notional"].items() if number(v)}
    books = {b: number(v) for b, v in row["book_gross_notional"].items() if number(v)}
    require(
        equity > 0 and gross >= 0 and all(v > 0 for v in (*asset.values(), *books.values())), "bad_exposure"
    )
    require(abs(sum(asset.values(), ZERO) - gross) <= EPS, "asset_gross_does_not_reconcile")
    require(abs(sum(books.values(), ZERO) - gross) <= EPS, "book_gross_does_not_reconcile")
    for index, expected in ((1, asset), (0, books)):
        actual = defaultdict(lambda: ZERO)
        for key, quantity in positions.items():
            require(key[1] in marks, "missing_mark_on_held_leg")
            actual[key[index]] += abs(quantity) * number(marks[key[1]])
        require(
            set(actual) == set(expected) and all(abs(actual[k] - v) <= EPS for k, v in expected.items()),
            "gross_exposure_does_not_reconcile_positions",
        )

    shares = {s: v / gross for s, v in asset.items()} if gross else {}
    return {
        "gross_over_equity": gross / equity,
        "asset_over_equity": {s: v / equity for s, v in asset.items()},
        "asset_gross_hhi": sum((s * s for s in shares.values()), ZERO) if shares else None,
        "book_gross_hhi": sum(((v / gross) ** 2 for v in books.values()), ZERO) if gross else None,
    }


def _event_parts(row, position, marks, prev, turnover):
    values = defaultdict(lambda: ZERO)
    for (book, symbol), quantity in position.items():
        if symbol not in marks or symbol not in prev:
            require(False, "missing_mark_on_held_leg")
        if marks[symbol] != prev[symbol]:
            values[book, symbol, "mark_pnl"] += quantity * (number(marks[symbol]) - number(prev[symbol]))
    if row["event"] in {"fill", "risk_fill"}:
        b, s = row["book"], row["symbol"]
        q, fill, ref, fee = (
            number(row[k]) for k in ("executed_delta", "execution_price", "reference_price", "fee")
        )
        require(q and fill > 0 and ref > 0 and fee >= 0 and s in marks, "invalid_execution_for_attribution")
        position[b, s] = position.get((b, s), ZERO) + q
        if not position[b, s]:
            del position[b, s]
        values[b, s, "reference_to_mark_pnl"] += q * (number(marks[s]) - ref)
        values[b, s, "execution_slippage"] += q * (ref - fill)
        values[b, s, "fees"] -= fee
        turnover[s] += abs(q) * fill
    elif row["event"] == "funding":
        b, s = row["book"], row["symbol"]
        require((b, s) in position and s in marks, "missing_funding_leg")
        expected = -position[b, s] * number(marks[s]) * number(row["rate"])
        require(
            number(row["settlement_mark"]) == number(marks[s])
            and abs(expected - number(row["cash_delta"])) <= EPS,
            "funding_does_not_reconcile",
        )
        values[b, s, "funding"] += number(row["cash_delta"])
    return values


def analyze(accounting, frames):
    """Reconcile each exact event; the closing path excludes the extra terminal open."""
    rows = accounting["ledger"]
    capital = number(accounting["initial_equity"])
    require(rows and capital > 0 and not _positions(rows[0]), "missing_flat_initial_boundary")
    sequence = tuple(dict.fromkeys(row["ts"] for row in rows))
    require(
        sequence == tuple(frames) and sequence == tuple(sorted(sequence)), "ledger_frame_calendar_mismatch"
    )
    require(all(type(ts) is int for ts in sequence), "invalid_ledger_timestamp")
    require(
        tuple(r["ts"] for r in rows if r["event"] == "frame_end") == sequence,
        "missing_or_duplicate_frame_end",
    )
    closes = {
        r["ts"] - DAY + 1: r["equity"] for r in rows if r["event"] == "frame_end" and r["ts"] % DAY == DAY - 1
    }
    path = daily_path(closes, capital, closes.values())
    require(sequence[0] == path["days"][0], "missing_initial_utc_open")
    require(sequence[-1] in {path["days"][-1] + DAY - 1, path["days"][-1] + DAY}, "invalid_terminal_boundary")
    cumulative, decline, worst, turnover = (defaultdict(lambda: ZERO) for _ in range(4))
    max_ratio, peak, previous_equity = ZERO, capital, capital
    first = {"seq": -1, "ts": sequence[0], "equity_usdt": capital}
    peak_point = worst_peak = worst_trough = first
    positions, event_audit, close_hhi = {}, [], []
    prev_marks = frames[sequence[0]]
    prior_ts = sequence[0]
    max_asset = defaultdict(lambda: ZERO)
    max_gross = max_hhi = max_book_hhi = ZERO
    max_error = ZERO
    for i, row in enumerate(rows):
        ts, equity = row["ts"], number(row["equity"])
        require(row["seq"] == i and ts >= prior_ts and equity > 0, "invalid_ledger_event_order_or_equity")
        event = _event_parts(row, positions, frames[ts], prev_marks, turnover)
        require(positions == _positions(row), "positions_do_not_reconcile")
        movement = sum(event.values(), ZERO)
        error = equity - previous_equity - movement
        require(abs(error) <= EPS, "event_equity_does_not_reconcile")
        max_error = max(max_error, abs(error))
        event_audit.append(
            {
                "seq": i,
                "ts": ts,
                "event": row["event"],
                "equity_usdt": equity,
                "delta_equity_usdt": equity - previous_equity,
                "residual_usdt": error,
                "components_usdt": _components(event),
            }
        )
        _add(cumulative, event)
        _add(decline, event)
        point = {"seq": i, "ts": ts, "equity_usdt": equity}
        if equity > peak:
            peak, peak_point = equity, point
            decline.clear()
        ratio = ONE - equity / peak
        if ratio > max_ratio:
            max_ratio, worst, worst_peak, worst_trough = ratio, decline.copy(), peak_point, point
        exposure = _concentration(row, positions, frames[ts])
        max_gross = max(max_gross, exposure["gross_over_equity"])
        for s, ratio in exposure["asset_over_equity"].items():
            max_asset[s] = max(max_asset[s], ratio)
        hhi, book_hhi = exposure["asset_gross_hhi"], exposure["book_gross_hhi"]
        if hhi is not None:
            max_hhi, max_book_hhi = max(max_hhi, hhi), max(max_book_hhi, book_hhi)
            if row["event"] == "frame_end" and ts % DAY == DAY - 1:
                close_hhi.append(hhi)
        prev_marks, previous_equity, prior_ts = frames[ts], equity, ts

    overall = _attributed(cumulative)
    drawdown = _attributed(worst)
    overall_error = previous_equity - capital - overall["net_usdt"]
    dd_error = worst_trough["equity_usdt"] - worst_peak["equity_usdt"] - drawdown["net_usdt"]
    require(abs(overall_error) <= EPS and abs(dd_error) <= EPS, "total_equity_does_not_reconcile")
    require(abs(previous_equity - number(accounting["equity"])) <= EPS, "terminal_equity_mismatch")
    amounts = overall["components_usdt"]
    for key, expected in (
        ("fees", -number(accounting["fees"])),
        ("funding", number(accounting["funding_cashflow"])),
        ("execution_slippage", -number(accounting["slippage_diagnostic"])),
    ):
        require(abs(amounts[key] - expected) <= EPS, "cost_components_do_not_reconcile")
    return {
        "path": path,
        "terminal": {
            "ts": sequence[-1],
            "equity_usdt": previous_equity,
            "return": previous_equity / capital - number(1),
            "open_legs": len(positions),
        },
        "turnover": {
            "absolute_fills_usdt": sum(turnover.values(), ZERO),
            "per_asset_absolute_fills_usdt": dict(sorted(turnover.items())),
            "over_initial_capital": sum(turnover.values(), ZERO) / capital,
        },
        "concentration": {
            "max_gross_over_equity": max_gross,
            "max_asset_over_equity": dict(sorted(max_asset.items())),
            "max_asset_gross_hhi": available(max_hhi) if max_asset else unavailable("no_gross_exposure"),
            "max_book_gross_hhi": available(max_book_hhi) if max_asset else unavailable("no_gross_exposure"),
            "mean_invested_daily_close_asset_hhi": (
                available(sum(close_hhi, ZERO) / len(close_hhi))
                if close_hhi
                else unavailable("no_invested_close")
            ),
        },
        "attribution": {
            "full_window": {**overall, "residual_usdt": overall_error},
            "maximum_event_drawdown": {
                **drawdown,
                "ratio": max_ratio,
                "peak": worst_peak,
                "trough": worst_trough,
                "residual_usdt": dd_error,
            },
            "max_event_residual_usdt": max_error,
            "events": event_audit,
        },
    }

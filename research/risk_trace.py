"""Per-event observed breaches and durations; no exemption or changed risk budget."""

from .engine import ZERO, number


def exposure_trace(ledger):
    trace, episodes = [], []
    episode = None
    previous_positions = {}
    for row in ledger:
        equity = number(row["equity"])
        gross = number(row["gross_notional"])
        assets = {s: number(n) for s, n in row["asset_absolute_notional"].items()}
        asset_limit = equity * number("0.25")
        gross_excess = max(ZERO, gross - equity)
        asset_excess = {s: max(ZERO, n - asset_limit) for s, n in assets.items()}
        breached = bool(gross_excess or any(asset_excess.values()))
        reducing = None
        if row["event"] in {"fill", "risk_fill"}:
            key = (row["book"], row["symbol"])
            old = previous_positions.get(key, ZERO)
            new = old + number(row["executed_delta"])
            reducing = abs(new) < abs(old) and (new == 0 or old * new > 0)
            if row["event"] == "risk_fill" and not reducing:
                raise AssertionError("Risk flatten increased or reversed a leg")
            if not reducing and breached:
                raise AssertionError("New/increasing fill breached original hard limits")
        record = {
            "seq": row["seq"],
            "ts": row["ts"],
            "event": row["event"],
            "equity": equity,
            "gross_notional": gross,
            "net_notional": row["net_notional"],
            "asset_absolute_notional": assets,
            "gross_limit": equity,
            "asset_limit": asset_limit,
            "gross_excess": gross_excess,
            "asset_excess": asset_excess,
            "hard_limit_breached": breached,
            "risk_exit_fill": row["event"] == "risk_fill",
            "reducing_existing_leg": reducing,
        }
        trace.append(record)
        if breached:
            if episode is None:
                episode = {
                    "start_ts": row["ts"],
                    "start_seq": row["seq"],
                    "peak_gross_excess": ZERO,
                    "peak_asset_excess": ZERO,
                    "breached_event_count": 0,
                }
            episode["peak_gross_excess"] = max(episode["peak_gross_excess"], gross_excess)
            episode["peak_asset_excess"] = max(
                episode["peak_asset_excess"], max(asset_excess.values(), default=ZERO)
            )
            episode["breached_event_count"] += 1
        elif episode is not None:
            episodes.append(
                {
                    **episode,
                    "restored_ts": row["ts"],
                    "restored_seq": row["seq"],
                    "duration_ms": row["ts"] - episode["start_ts"],
                    "right_censored": False,
                }
            )
            episode = None
        previous_positions = {(p["book"], p["symbol"]): number(p["quantity"]) for p in row["positions"]}
    if episode is not None:
        episodes.append(
            {
                **episode,
                "restored_ts": None,
                "restored_seq": None,
                "duration_ms": ledger[-1]["ts"] - episode["start_ts"],
                "right_censored": True,
            }
        )
    return {
        "label": "Observed breach, including sequential reductions; not compliant during breach",
        "duration_scope": "synthetic timestamp grid; intratick event order retained, not latency guarantee",
        "events": trace,
        "episodes": episodes,
    }

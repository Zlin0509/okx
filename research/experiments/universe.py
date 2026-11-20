"""Only the driver owns metadata histories; strategy receives detached point-in-time pool results."""

from itertools import combinations

from ..engine import number
from ..portfolio import DAY
from .contract import protocol


def pool_at(ts, metadata, turnover, observations, *, universe_verified):
    p = protocol()["pool"]
    warmup = max(protocol()["statistics"]["qualification_window"], 112)
    close = ts - 1 if ts % DAY == 0 else ts
    lag_start = 0 if ts % DAY == 0 else 1
    unknown, rejected, qualified = {}, {}, []
    if universe_verified is not True:
        return {
            "status": "unknown",
            "members": [],
            "unknown": {"universe": ["historical_universe_unknown"]},
            "excluded": {},
        }
    for symbol in sorted(metadata):
        known = [m for m in metadata[symbol] if m["known_at"] <= ts and m["effective_at"] <= ts]
        meta = max(known, key=lambda m: (m["effective_at"], m["known_at"])) if known else {}
        if meta.get("verified") is not True or meta.get("listing") is None or meta.get("continuous") is None:
            unknown[symbol] = ["historical_tradability_or_listing_unknown"]
            continue
        reasons = []
        if meta.get("tradable") is not True or (
            meta.get("delisting") is not None and ts >= meta["delisting"]
        ):
            reasons.append("not_tradable")
        first = ((max(meta["listing"], meta["continuous"]) + DAY - 1) // DAY) * DAY + DAY - 1
        if ts < first + (p["age_days"] + warmup) * DAY:
            reasons.append("age_or_max_signal_warmup_not_met")
        values = []
        for lag in range(lag_start, lag_start + 30):
            t = close - lag * DAY
            row = turnover.get(symbol, {}).get(t)
            if row is None or row.get("available_at", ts + 1) > ts or str(row.get("confirm")) != "1":
                reasons.append("missing_or_unavailable_lagged_volCcyQuote")
                continue
            try:
                value = number(row["volCcyQuote"])
                if value < 0:
                    raise ValueError
                values.append(value)
            except (ValueError, KeyError):
                reasons.append("invalid_lagged_volCcyQuote")
        for lag in range(warmup + 1):
            row = observations.get(close - lag * DAY)
            if row is None or row.available_at > ts or symbol not in row.prices:
                reasons.append("missing_or_unavailable_common_price_window")
                break
        mean = sum(values, number(0)) / 30 if len(values) == 30 else None
        if mean is not None and mean < number(p["minimum_mean_usdt"]):
            reasons.append("below_lagged_liquidity_floor")
        if reasons:
            rejected[symbol] = sorted(set(reasons))
        else:
            qualified.append((symbol, mean))
    selected = sorted(qualified, key=lambda item: (-item[1], item[0]))[: p["target_assets"]]
    status = "unknown" if unknown else "eligible" if len(selected) >= p["minimum_assets"] else "excluded"
    return {
        "status": status,
        "members": [s for s, _ in selected] if status == "eligible" else [],
        "ranking": [{"symbol": s, "lagged_mean_usdt": str(v)} for s, v in selected],
        "unknown": unknown,
        "excluded": rejected,
        "universe_verified": universe_verified,
    }


def candidate_family(members, anchors):
    members = tuple(sorted(members))
    family = [{"id": "pair_" + a + "_" + b, "symbols": (a, b)} for a, b in combinations(members, 2)]
    if set(anchors) <= set(members):
        family += [{"id": "basket_" + s, "symbols": (s, *anchors)} for s in members if s not in anchors]
    return family

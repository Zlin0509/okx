"""Point-in-time lifecycle, declared candidate families and calendar joins."""

from collections import defaultdict

from ..engine import number
from ..execution_audit import canonical, sha
from ..experiments.universe import candidate_family
from .calendar import intervals, missing_coverage
from .common import ONE_DAY, check, day, days, instant, midnight, unique
from .intake import witness

ANCHORS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


def universe(body, src):
    witness(src, body["source_id"], "historical_instrument_events")
    check(body["scope"] == "global_historical", "history_scope_not_global")
    perps, spots = body["perpetuals"], body["spots"]
    check(
        all(s.endswith("-USDT-SWAP") for s in perps) and all(s.endswith("-USDT") for s in spots),
        "invalid_instrument_type",
    )
    all_symbols = perps + spots
    check(len(set(all_symbols)) == len(all_symbols), "duplicate_inventory_instrument")
    schedules = defaultdict(list)
    for row in body["lifecycle"]:
        symbol, kind = row["instrument"], row["event"]
        captured = witness(src, row["source_id"], "historical_instrument_events")
        check(symbol in all_symbols, "lifecycle_instrument_outside_inventory", symbol)
        check(kind in {"listed", "delisted"}, "unsupported_or_unknown_lifecycle_event", symbol)
        for field in ("published_at", "available_at", "effective_at"):
            check(row.get(field) is not None, "unknown_lifecycle_time", f"{symbol} {field}")
        published, available, effective = (
            instant(row[field]) for field in ("published_at", "available_at", "effective_at")
        )
        check(published <= available <= captured, "invalid_publication_availability_order", symbol)
        schedules[symbol].append((effective, available, kind))
    for symbol in all_symbols:
        events = sorted(schedules.get(symbol, []))
        check(bool(events), "missing_instrument_lifecycle", symbol)
        check(
            all(a[0] < b[0] for a, b in zip(events, events[1:], strict=False)),
            "duplicate_lifecycle_effective_time",
            symbol,
        )
        check(
            all(event[2] == ("delisted" if i % 2 else "listed") for i, event in enumerate(events)),
            "invalid_listing_delisting_sequence",
            symbol,
        )
        schedules[symbol] = events
    return {
        "schedules": schedules,
        "perps": set(perps),
        "spots": set(spots),
        "daily_market_coverage": intervals(body["perpetual_daily_open_close_coverage"]),
    }


def listing_at(u, symbol, ts):
    effective = [row for row in u["schedules"].get(symbol, ()) if row[0] <= ts]
    if not effective:
        return None, "not_yet_listed"
    affected = effective[-1]
    if affected[1] > ts:
        return None, "lifecycle_not_known_at_decision"
    if affected[2] != "listed":
        return None, "instrument_not_tradable"
    return affected[0], None


def membership_commitment(members, configs):
    family = candidate_family(members, ANCHORS)
    tests = []
    for cfg in configs:
        for c in family:
            prefix = cfg + "/" + c["id"] + "/"
            tests += [prefix + mode for mode in ("eg", "cusum", "wald")]
            tests += [prefix + f"{mode}:{s}" for mode in ("level", "diff") for s in c["symbols"]]
    n, legs = len(family), sum(len(c["symbols"]) for c in family)
    return {
        "candidate_ids": sorted(c["id"] for c in family),
        "counts": {"raw": 18 * n + 12 * legs, "primary_holm": 18 * n + 6 * legs, "BY_EG": 6 * n},
        "test_ids_sha256": sha(canonical(sorted(tests))),
    }


def decisions(body, cal, u, src, protocol, findings, artifact):
    witness(src, body["source_id"], "pre_statistics_registry")
    configs = [c["id"] for c in protocol["configs"]]
    check(body["configuration_ids"] == configs, "six_configuration_budget_mismatch")
    planned = unique(body["decisions"], lambda row: day(row["date"]), "duplicate_weekly_decision")
    expected = {d for d in cal["all"] if d.weekday() == 0}
    for d in sorted(expected - planned.keys()):
        findings.add(artifact, "missing_weekly_decision", d.isoformat())
    for d in sorted(planned.keys() - expected):
        findings.add(artifact, "unexpected_weekly_decision", d.isoformat())
    market, turnover, cost_days = (defaultdict(set) for _ in range(3))
    included = set()
    all_slots = u["perps"]
    warmup = protocol["statistics"]["qualification_window"] + 1
    for d in sorted(expected & planned.keys()):
        row = planned[d]
        members = row["members"]
        count = len(set(members))
        check(count == len(members) and 6 <= count <= 8, "invalid_candidate_pool_size", d.isoformat())
        excluded = row["excluded"]
        check(
            set(members).isdisjoint(excluded) and (set(members) | set(excluded)) == all_slots,
            "incomplete_candidate_inventory",
            d.isoformat(),
        )
        check(
            all(isinstance(reason, str) and bool(reason.strip()) for reason in excluded.values()),
            "missing_candidate_exclusion_reason",
            d.isoformat(),
        )
        for field, value in membership_commitment(members, configs).items():
            if row[field] != value:
                findings.add(artifact, "candidate_family_commitment_mismatch", f"{d.isoformat()} {field}")
        for symbol in sorted(all_slots):
            listed, reason = listing_at(u, symbol, midnight(d))
            if reason == "lifecycle_not_known_at_decision":
                findings.add(artifact, reason, f"{symbol} {d.isoformat()}")
            if symbol in members:
                earliest = warmup + protocol["pool"]["age_days"]
                first_full_day = (
                    None
                    if listed is None
                    else listed.date() + (ONE_DAY if listed != midnight(listed.date()) else ONE_DAY * 0)
                )
                if first_full_day is None or (d - first_full_day).days < earliest:
                    findings.add(
                        artifact, "candidate_listing_or_warmup_not_proven", f"{symbol} {d.isoformat()}"
                    )
                market[symbol].update(days(d - ONE_DAY * warmup, d - ONE_DAY))
            if listed is not None:
                turnover[symbol].update(days(d - 30 * ONE_DAY, d - ONE_DAY))
        included.update(members)
    # Include every live day for a declared leg, including days it may carry between rebalances.
    for symbol in included:
        for d in cal["all"]:
            listed, reason = listing_at(u, symbol, midnight(d))
            if reason == "lifecycle_not_known_at_decision":
                findings.add(artifact, reason, f"{symbol} {d.isoformat()}")
            if listed is not None:
                market[symbol].add(d)
                cost_days[symbol].add(d)
                turnover[symbol].update(days(d - 30 * ONE_DAY, d - ONE_DAY))
    return {"cost_days": cost_days, "markets": market, "turnover": turnover, "decisions": expected}


def venue(body, req, src, findings, artifact):
    witness(src, body["source_id"], "venue_and_turnover_calendar")
    coverage = intervals(body["confirmed_volCcyQuote_coverage"])
    missing_coverage(coverage, req["turnover"], findings, artifact, "missing_lagged_turnover_day")
    for symbol in sorted(req["cost_days"]):
        metadata = body["instrument_rules"].get(symbol)
        if metadata is None:
            findings.add(artifact, "missing_venue_rule", symbol)
            continue
        if metadata["unit"] != "USDT-linear" or any(
            number(metadata[k]) <= 0 for k in ("lot_size", "tick_size")
        ):
            findings.add(artifact, "invalid_venue_rule", symbol)
    missing_coverage(
        intervals(body["rule_coverage"]),
        req["cost_days"],
        findings,
        artifact,
        "missing_effective_venue_rule_day",
    )


def spot(body, cal, u, req, src, findings, artifact):
    witness(src, body["source_id"], "spot_baseline_calendar")
    planned = unique(body["selections"], lambda row: day(row["date"]), "duplicate_spot_decision")
    targets = defaultdict(set)
    for d in cal["all"]:
        targets["BTC-USDT"].add(d)
    for d in sorted(req["decisions"] - planned.keys()):
        findings.add(artifact, "missing_spot_baseline_selection", d.isoformat())
    for d in sorted(planned.keys() - req["decisions"]):
        findings.add(artifact, "unexpected_spot_baseline_selection", d.isoformat())
    evaluation_days = set(cal["all"])
    for d in sorted(planned.keys() & req["decisions"]):
        selections = planned[d]["spot_members"]
        check(
            6 <= len(selections) <= 8 and len(set(selections)) == len(selections),
            "invalid_spot_baseline_selection",
            d.isoformat(),
        )
        for symbol in sorted(selections):
            since, _ = listing_at(u, symbol + "-SWAP", midnight(d))
            listed, _ = listing_at(u, symbol, midnight(d))
            check(
                symbol in u["spots"]
                and since is not None
                and listed is not None
                and (midnight(d) - since).days >= 180,
                "spot_baseline_member_ineligible_at_decision",
                f"{symbol} {d.isoformat()}",
            )
            # Tuesday open through the next Tuesday rebalance, where an old leg may be sold.
            targets[symbol].update(evaluation_days.intersection(days(d + ONE_DAY, d + 8 * ONE_DAY)))
    missing_coverage(
        intervals(body["spot_daily_open_close_coverage"]),
        targets,
        findings,
        artifact,
        "missing_spot_baseline_day",
    )

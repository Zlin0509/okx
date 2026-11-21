"""Frozen date boundaries, exposure checks and coverage utilities."""

from .common import ONE_DAY, check, day, days, instant
from .intake import witness


def calendar(material, registration, as_of, src, findings, artifact):
    witness(src, material["source_id"], "eligible_calendar")
    r = registration["calendar"]
    selection = material["test_window"]
    check(
        selection in ("original_locked_test", "future_reserve_if_original_test_invalid"),
        "unregistered_test_window",
        selection,
    )
    if selection == "future_reserve_if_original_test_invalid":
        check(
            material.get("original_test_disposition") == "invalid_after_independent_audit",
            "reserve_requires_original_test_disposition",
        )
    begin, end = map(day, r[selection])
    test = tuple(map(day, material["eligible_test_dates"]))
    check(test and tuple(sorted(set(test))) == test, "invalid_sorted_unique_eligible_dates")
    check(begin <= test[0] <= test[-1] <= end, "eligible_dates_outside_frozen_window")
    check(test[-1] < as_of.date(), "eligible_date_not_complete_at_audit_cutoff")
    if any(b - a != ONE_DAY for a, b in zip(test, test[1:], strict=False)):
        findings.add(artifact, "noncontiguous_eligible_dates", "R3 cannot splice missing UTC days")
    floor = registration["confirmatory_gates"]["minimum_eligible_test_days"]
    if len(test) < floor:
        findings.add(artifact, "insufficient_eligible_test_days", f"{len(test)}/{floor}")
    q_start, q_end = map(day, r["quarantine"])
    for d in test:
        if q_start <= d <= q_end:
            findings.add(artifact, "quarantined_test_date", d.isoformat())
    train, validation = (tuple(days(*map(day, r[phase]))) for phase in ("training", "validation"))
    return {
        "test_window": selection,
        "train": train,
        "validation": validation,
        "test": test,
        "all": (*train, *validation, *test),
        "audit_window": (begin, end),
    }


def exposures(material, cal, as_of, src, findings, artifact):
    captured = witness(src, material["source_id"], "exposure_ledger")
    check(material["inventory_status"] == "complete_for_declared_scope", "exposure_inventory_incomplete")
    audited = instant(material["audited_through_utc"])
    check(audited == as_of and captured == audited, "exposure_audit_cutoff_mismatch")
    begin, end = map(day, material["audited_test_window"])
    check(
        begin <= cal["audit_window"][0] and end >= cal["test"][-1],
        "exposure_inventory_does_not_cover_test_dates",
    )
    known_types = {
        "market_prices",
        "funding",
        "settlement_marks",
        "historical_metadata",
        "test_evaluation",
        "performance_selection",
        "other",
    }
    for row in material["exposures"]:
        check(row["kind"] in known_types, "unknown_exposure_kind")
        check(instant(row["observed_at"]) <= audited, "exposure_after_audit_cutoff")
        if row.get("first_date") is None or row.get("last_date") is None:
            findings.add(artifact, "exposure_dates_unknown", row["kind"])
            continue
        first, last = day(row["first_date"]), day(row["last_date"])
        check(first <= last, "inverted_exposure_interval")
        for d in cal["test"]:
            if first <= d <= last:
                findings.add(artifact, "eligible_date_has_prior_exposure", f"{d.isoformat()} {row['kind']}")


def intervals(table):
    index = {}
    for key, spans in table.items():
        parsed = [tuple(map(day, pair)) for pair in spans]
        check(all(a <= b for a, b in parsed), "inverted_coverage_interval", key)
        check(
            all(a[1] < b[0] for a, b in zip(parsed, parsed[1:], strict=False)),
            "overlapping_coverage_intervals",
            key,
        )
        index[key] = parsed
    return index


def missing_coverage(coverage, requirements, findings, material, code):
    for symbol, expected in sorted(requirements.items()):
        spans = coverage.get(symbol, [])
        for d in sorted(expected):
            if not any(start <= d <= stop for start, stop in spans):
                findings.add(material, code, f"{symbol} {d.isoformat()}")

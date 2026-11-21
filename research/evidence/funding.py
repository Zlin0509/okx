"""Reconcile an independently sourced cadence against actual per-event rate and mark records."""

from collections import defaultdict
from datetime import timedelta

from ..engine import number
from .common import ONE_DAY, check, instant, midnight
from .intake import witness


def _schedule(material, src):
    grouped = defaultdict(list)
    for row in material["cadence"]:
        witness(src, row["source_id"], "settlement_schedule")
        begin, end, anchor = (
            instant(row[k]) for k in ("start_utc", "end_exclusive_utc", "first_scheduled_utc")
        )
        seconds = row["interval_seconds"]
        check(type(seconds) is int and seconds > 0, "invalid_settlement_interval", row["instrument"])
        step = timedelta(seconds=seconds)
        check(
            begin <= anchor < end and anchor - begin < step,
            "invalid_settlement_cadence_anchor",
            row["instrument"],
        )
        grouped[row["instrument"]].append((begin, end, anchor, step))
    for symbol, rows in grouped.items():
        rows.sort()
        check(
            all(a[1] <= b[0] for a, b in zip(rows, rows[1:], strict=False)),
            "overlapping_settlement_cadence",
            symbol,
        )
    return grouped


def _records(rows, src, as_of, *, marks, findings, artifact):
    index, seen, event_ids = {}, set(), set()
    for row in rows:
        key = (row["instrument"], instant(row["scheduled_at"]))
        if key in seen:
            findings.add(artifact, "duplicate_settlement_key", f"{'mark' if marks else 'rate'} {key}")
            continue
        seen.add(key)
        captured = witness(
            src, row["source_id"], "exact_settlement_marks" if marks else "settled_funding_records"
        )
        actual = instant(row["settled_at"])
        check(actual <= captured <= as_of and key[1] <= captured, "unproven_settlement_time", str(key))
        event_id = row["event_id"]
        check(
            isinstance(event_id, str) and bool(event_id.strip()),
            "missing_exchange_settlement_event_id",
            str(key),
        )
        check((key[0], event_id) not in event_ids, "duplicate_exchange_settlement_event_id", event_id)
        event_ids.add((key[0], event_id))
        if marks:
            check(
                row["mark_kind"] == "exact_applied_settlement_mark",
                "mark_is_not_exact_settlement_price",
                str(key),
            )
            check(instant(row["sampled_at"]) <= actual, "mark_sample_after_settlement", str(key))
            check(number(row["mark_price"]) > 0, "invalid_settlement_mark", str(key))
        else:
            check(row["rate_kind"] == "actual_settled_rate", "rate_is_not_actual_settled_rate", str(key))
            number(row["rate"])  # Signed and zero rates are valid; null and nonfinite are not.
        index[key] = (event_id, actual)
    return index


def _expected(groups, cost_days, findings, artifact):
    expected = set()
    for symbol, calendar in sorted(cost_days.items()):
        for d in sorted(calendar):
            start, end = midnight(d), midnight(d + ONE_DAY)
            cursor = start
            for left, right, anchor, step in groups.get(symbol, []):
                if right <= start or left >= end:
                    continue
                low, high = max(left, start), min(right, end)
                if low != cursor:
                    findings.add(
                        artifact, "missing_settlement_cadence_coverage", f"{symbol} {cursor.isoformat()}"
                    )
                first = max(0, (low - anchor) // step)
                t = anchor + first * step
                if t < low:
                    t += step
                while t < high:
                    expected.add((symbol, t))
                    t += step
                cursor = high
            if cursor != end:
                findings.add(
                    artifact, "missing_settlement_cadence_coverage", f"{symbol} {cursor.isoformat()}"
                )
    return expected


def settlement(body, cost_days, src, as_of, findings, artifact):
    cadence = _schedule(body, src)
    expected = _expected(cadence, cost_days, findings, artifact)
    rates = _records(body["rates"], src, as_of, marks=False, findings=findings, artifact=artifact)
    marks = _records(body["marks"], src, as_of, marks=True, findings=findings, artifact=artifact)
    for key in sorted(expected):
        if key not in rates:
            findings.add(artifact, "missing_settled_rate", str(key))
        if key not in marks:
            findings.add(artifact, "missing_exact_settlement_mark", str(key))
    for name, records in (("rate", rates), ("mark", marks)):
        for key in sorted(records.keys() - expected):
            # Other verified collections outside the declared scoring scope do not expand it.
            if key[1].date() in cost_days.get(key[0], ()):
                findings.add(artifact, "unexpected_settlement_point_inside_scope", f"{name} {key}")
    matched = 0
    for key in sorted(expected & rates.keys() & marks.keys()):
        if rates[key] != marks[key]:
            findings.add(artifact, "settlement_event_id_or_actual_time_mismatch", str(key))
        else:
            matched += 1
    return {"expected": len(expected), "paired": matched}

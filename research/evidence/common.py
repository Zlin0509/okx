"""Parsing and deterministic diagnostics for evidence manifests."""

import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

HEX = re.compile(r"[a-f0-9]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
UTC_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
ONE_DAY = timedelta(days=1)


class Invalid(ValueError):
    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail
        super().__init__(code)


def check(condition, code, detail=""):
    if not condition:
        raise Invalid(code, detail)


def instant(value):
    check(
        isinstance(value, str) and bool(UTC_INSTANT.fullmatch(value)), "expected_explicit_utc_instant", value
    )
    return datetime.fromisoformat(value[:-1] + "+00:00")


def day(value):
    check(isinstance(value, str), "expected_utc_date", value)
    parsed = date.fromisoformat(value)
    check(parsed.isoformat() == value, "expected_utc_date", value)
    return parsed


def days(first, last):
    return (first + i * ONE_DAY for i in range((last - first).days + 1))


def midnight(d):
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def unique(rows, key, code):
    index = {}
    for row in rows:
        k = key(row)
        check(k not in index, code, str(k))
        index[k] = row
    return index


def decode(raw):
    def pairs(items):
        return unique(items, lambda kv: kv[0], "duplicate_json_key")

    def reject_constant(value):
        raise Invalid("invalid_json_number", value)

    # The pairs hook checks duplicates before creating the value dictionary.
    return json.loads(
        raw,
        object_pairs_hook=lambda rows: {k: v for k, v in pairs(rows).values()},
        parse_constant=reject_constant,
    )


class Findings:
    def __init__(self):
        self.data = defaultdict(lambda: {"count": 0, "examples": []})

    def add(self, artifact, code, detail=""):
        row = self.data[artifact, code]
        detail = str(detail)
        row["count"] += 1
        if detail not in row["examples"] and len(row["examples"]) < 6:
            row["examples"].append(detail)

    def invalid(self, artifact, error):
        if isinstance(error, Invalid):
            self.add(artifact, error.code, error.detail)
        elif isinstance(error, KeyError):
            self.add(artifact, "missing_required_field", str(error.args[0]))
        else:
            self.add(artifact, "invalid_evidence_shape", type(error).__name__)

    def result(self):
        return [{"artifact": key[0], "code": key[1], **row} for key, row in sorted(self.data.items())]


INPUT_ERRORS = (ValueError, TypeError, KeyError, AttributeError, OverflowError)

"""Build deterministic, visibly fabricated full-calendar evidence and fault cases."""

import argparse
import json
from datetime import timedelta
from pathlib import Path

from ..execution_audit import canonical, sha
from ..experiments.contract import protocol
from .common import ONE_DAY, day, days, midnight
from .coverage import membership_commitment
from .intake import SOURCE_KINDS
from .validator import (
    APPROVAL,
    DATES,
    EXPOSURE,
    FAMILY,
    FREEZE,
    FUNDING,
    SPOT,
    UNIVERSE,
    VENUE,
    policy_anchors,
)

ORIGIN = "synthetic_test"
CUTOFF = "2026-11-21T08:00:00Z"
FIRST, LAST = day("2021-01-01"), day("2025-12-31")
SPOTS = ["BTC-USDT", "ETH-USDT", *(f"MOCK{i}-USDT" for i in range(4))]
PERPS = [s + "-SWAP" for s in SPOTS]


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make():
    root = {
        "schema": "zlin11-evidence-v1",
        "origin": ORIGIN,
        "as_of_utc": CUTOFF,
        "prepared_by": "fixture-author",
        "policy": policy_anchors(),
    }
    config_ids = [c["id"] for c in protocol()["configs"]]
    first, last = FIRST.isoformat(), LAST.isoformat()
    all_days = tuple(days(FIRST, LAST))
    settle, rates, marks = [], [], []
    exceptional = day("2024-03-17")

    def add_cadence(symbol, start, end, hours):
        settle.append(
            {
                "instrument": symbol,
                "start_utc": iso(midnight(start)),
                "end_exclusive_utc": iso(midnight(end)),
                "first_scheduled_utc": iso(midnight(start) + timedelta(hours=4)),
                "interval_seconds": hours * 3600,
                "source_id": "settlement_schedule",
            }
        )

    for symbol in PERPS:
        if symbol == PERPS[0]:
            add_cadence(symbol, FIRST, exceptional, 24)
            add_cadence(symbol, exceptional, exceptional + ONE_DAY, 8)
            add_cadence(symbol, exceptional + ONE_DAY, LAST + ONE_DAY, 24)
        else:
            add_cadence(symbol, FIRST, LAST + ONE_DAY, 24)
        for d in all_days:
            starts = [4, 12, 20] if d == exceptional and symbol == PERPS[0] else [4]
            for h in starts:
                planned = midnight(d) + timedelta(hours=h)
                actual = planned + timedelta(seconds=22)
                row = {
                    "instrument": symbol,
                    "scheduled_at": iso(planned),
                    "settled_at": iso(actual),
                    "event_id": f"mock-{d.isoformat()}-{h}",
                }
                rates.append(
                    row
                    | {
                        "rate_kind": "actual_settled_rate",
                        "rate": "-0.001" if h == 4 else "0",
                        "source_id": "settled_funding_records",
                    }
                )
                marks.append(
                    row
                    | {
                        "mark_kind": "exact_applied_settlement_mark",
                        "mark_price": "100",
                        "sampled_at": iso(actual - timedelta(seconds=1)),
                        "source_id": "exact_settlement_marks",
                    }
                )
    mondays = [d.isoformat() for d in all_days if d.weekday() == 0]
    membership = {"members": PERPS, "excluded": {}, **membership_commitment(PERPS, config_ids)}
    materials = {
        DATES: {
            "source_id": "eligible_calendar",
            "test_window": "original_locked_test",
            "eligible_test_dates": [t.isoformat() for t in days(day("2025-01-01"), LAST)],
        },
        UNIVERSE: {
            "source_id": "historical_instrument_events",
            "scope": "global_historical",
            "perpetuals": PERPS,
            "spots": SPOTS,
            "lifecycle": [
                {
                    "instrument": s,
                    "event": "listed",
                    "published_at": "2018-01-01T00:00:00Z",
                    "available_at": "2018-01-02T00:00:00Z",
                    "effective_at": "2018-01-03T00:00:00Z",
                    "source_id": "historical_instrument_events",
                }
                for s in (*PERPS, *SPOTS)
            ],
            "perpetual_daily_open_close_coverage": dict.fromkeys(PERPS, [["2019-07-01", last]]),
        },
        FAMILY: {
            "source_id": "pre_statistics_registry",
            "configuration_ids": config_ids,
            "decisions": [dict(membership, date=d) for d in mondays],
        },
        EXPOSURE: {
            "source_id": "exposure_ledger",
            "inventory_status": "complete_for_declared_scope",
            "audited_through_utc": CUTOFF,
            "audited_test_window": ["2025-01-01", "2026-10-31"],
            "exposures": [],
        },
        FUNDING: {"cadence": settle, "rates": rates, "marks": marks},
        SPOT: {
            "source_id": "spot_baseline_calendar",
            "spot_daily_open_close_coverage": dict.fromkeys(SPOTS, [[first, last]]),
            "selections": [{"date": d, "spot_members": SPOTS} for d in mondays],
        },
        VENUE: {
            "source_id": "venue_and_turnover_calendar",
            "confirmed_volCcyQuote_coverage": dict.fromkeys(PERPS, [["2020-11-01", last]]),
            "rule_coverage": dict.fromkeys(PERPS, [[first, last]]),
            "instrument_rules": dict.fromkeys(
                PERPS, {"unit": "USDT-linear", "lot_size": "1", "tick_size": "0.1"}
            ),
        },
        FREEZE: {"policy": root["policy"], "configuration_ids": config_ids},
    }
    src = {
        name: {
            "id": name,
            "origin": ORIGIN,
            "kind": name,
            "reference": "FABRICATED source witness " + name,
            "captured_at": CUTOFF if name != "independent_review_receipt" else "2026-11-21T09:00:00Z",
        }
        for name in sorted(SOURCE_KINDS)
    }
    return root, materials, src


def seal(directory, root, materials, src, *, receipt=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sources").mkdir(exist_ok=True)
    index = []
    for name, doc in src.items():
        raw = canonical(doc)
        (directory / "sources" / (name + ".json")).write_bytes(raw)
        index.append({"id": name, "kind": doc["kind"], "sha256": sha(raw)})
    files = {name: canonical({"origin": root["origin"], **body}) for name, body in materials.items()}
    files["sources"] = canonical({"sources": index})
    envelope = {**root, "files": {name: sha(raw) for name, raw in files.items()}}
    if receipt is None:
        receipt = {
            "reviewer_id": "fixture-reviewer",
            "reviewed_at": "2026-11-21T08:30:00Z",
            "decision": "approved_for_registered_scope",
            "source_id": "independent_review_receipt",
            "evidence_binding_sha256": sha(canonical(envelope)),
        }
    if receipt is not False:
        files[APPROVAL] = canonical({"origin": root["origin"], **receipt})
        envelope["files"][APPROVAL] = sha(files[APPROVAL])
    for name, raw in files.items():
        (directory / (name + ".json")).write_bytes(raw)
    raw = canonical(envelope)
    (directory / "dossier.json").write_bytes(raw)
    return sha(raw)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    digest = seal(args.directory, *make())
    print(json.dumps({"origin": ORIGIN, "dossier_sha256": digest}, sort_keys=True))


if __name__ == "__main__":
    main()

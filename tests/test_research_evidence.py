"""Synthetic evidence acceptance and failure cases without market inputs."""

from copy import copy
from datetime import timedelta

import pytest

from research.evidence.common import Findings, Invalid, day, decode, instant
from research.evidence.coverage import listing_at, universe
from research.evidence.fixture import CUTOFF, PERPS, make, seal
from research.evidence.funding import settlement
from research.evidence.validator import (
    APPROVAL,
    DATES,
    EXPOSURE,
    FAMILY,
    FREEZE,
    FUNDING,
    SPOT,
    UNIVERSE,
    VENUE,
    audit,
)
from research.experiments.contract import real_preflight


def codes(report):
    return {(row["artifact"], row["code"]): row["count"] for row in report["findings"]}


@pytest.fixture(scope="module")
def complete(tmp_path_factory):
    directory = tmp_path_factory.mktemp("synthetic-evidence")
    root, materials, sources = make()
    digest = seal(directory, root, materials, sources)
    return directory, digest


def test_valid_full_calendar_and_variable_settlement_still_cannot_trade(complete):
    result = audit(*complete)
    assert result["status"] == "synthetic_evidence_consistent", result["findings"]
    assert result["findings"] == []
    assert result["verified_source_count"] == 10
    assert result["calendar"]["test_days"] == 365
    assert result["settlements"] == {"expected": 10958, "paired": 10958}
    assert set(result["materials"].values()) == {"validated"}
    assert result["real_execution_allowed"] is False and result["real_returns"] is None
    legacy = real_preflight({name: {"reviewed": True} for name in result["materials"]})
    assert legacy["status"] == "refused" and legacy["real_returns"] is None


def test_external_hash_blocks_member_parsing(complete):
    directory, _ = complete
    result = audit(directory, "0" * 64)
    assert result["origin"] is None
    assert codes(result)["dossier", "sha256_mismatch"] == 1
    assert len(result["materials"]) == 9 and set(result["materials"].values()) == {"unavailable"}


def test_duplicate_json_fields_rejected():
    with pytest.raises(Invalid, match="duplicate_json_key"):
        decode(b'{"sha":"a","sha":"b"}')


def test_missing_root_is_structured(tmp_path):
    result = audit(tmp_path, "0" * 64)
    assert codes(result)["dossier", "missing_evidence_file"] == 1
    assert result["status"] == "evidence_incomplete"


def test_member_and_source_byte_tampering_and_missing_approval(tmp_path):
    root, materials, sources = make()
    digest = seal(tmp_path, root, materials, sources, receipt=False)
    p = tmp_path / "sources" / "settlement_schedule.json"
    p.write_bytes(p.read_bytes() + b" ")
    p = tmp_path / (VENUE + ".json")
    p.write_bytes(p.read_bytes() + b" ")
    result = audit(tmp_path, digest)
    failures = codes(result)
    assert failures["sources", "sha256_mismatch"] == 1
    assert failures[VENUE, "sha256_mismatch"] == 1
    assert failures[APPROVAL, "missing_material_in_file_index"] == 1
    assert failures[FUNDING, "unverified_source_reference"] == 1
    assert result["status"] == "evidence_incomplete"


def test_lifecycle_point_in_time_and_unknown_effective_time():
    _, body, _ = make()
    u = copy(body[UNIVERSE])
    u["lifecycle"] = [dict(r) for r in u["lifecycle"]]
    src = {"historical_instrument_events": ("historical_instrument_events", instant(CUTOFF))}
    asset = u["lifecycle"][0]
    asset.update(
        published_at="2021-02-01T00:00:00Z",
        available_at="2021-02-02T00:00:00Z",
        effective_at="2021-01-02T00:00:00Z",
    )
    validated = universe(u, src)
    assert listing_at(validated, asset["instrument"], instant("2021-01-04T00:00:00Z")) == (
        None,
        "lifecycle_not_known_at_decision",
    )
    assert listing_at(validated, asset["instrument"], instant("2021-02-02T00:00:00Z"))[0] == instant(
        "2021-01-02T00:00:00Z"
    )
    asset["available_at"] = "2021-01-02T00:00:00Z"
    with pytest.raises(Invalid, match="invalid_publication_availability_order"):
        universe(u, src)
    asset["effective_at"] = None
    with pytest.raises(Invalid, match="unknown_lifecycle_time"):
        universe(u, src)


def test_cross_material_gaps_have_distinct_reasons(tmp_path):
    root, materials, sources = make()
    materials[UNIVERSE]["perpetual_daily_open_close_coverage"][PERPS[0]] = [
        ["2019-07-01", "2025-05-31"],
        ["2025-06-02", "2025-12-31"],
    ]
    materials[VENUE]["confirmed_volCcyQuote_coverage"][PERPS[0]] = [
        ["2020-11-01", "2025-05-31"],
        ["2025-06-02", "2025-12-31"],
    ]
    materials[SPOT]["spot_daily_open_close_coverage"]["BTC-USDT"] = [
        ["2021-01-01", "2025-05-31"],
        ["2025-06-02", "2025-12-31"],
    ]
    materials[VENUE]["rule_coverage"][PERPS[0]] = [["2021-01-01", "2025-05-31"], ["2025-06-02", "2025-12-31"]]
    row = materials[FAMILY]["decisions"][0]
    row["test_ids_sha256"] = "0" * 64
    result = audit(tmp_path, seal(tmp_path, root, materials, sources))
    c = codes(result)
    assert c[UNIVERSE, "missing_perpetual_open_close_day"] == 1
    assert c[VENUE, "missing_lagged_turnover_day"] == 1
    assert c[VENUE, "missing_effective_venue_rule_day"] == 1
    assert c[SPOT, "missing_spot_baseline_day"] == 1
    assert c[FAMILY, "candidate_family_commitment_mismatch"] == 1


def test_exposure_unknown_and_overlap_are_not_clean_oos(tmp_path):
    root, materials, sources = make()
    materials[EXPOSURE]["exposures"] = [
        {
            "kind": "historical_metadata",
            "first_date": "2025-06-04",
            "last_date": "2025-06-05",
            "observed_at": "2026-11-20T00:00:00Z",
        },
        {"kind": "test_evaluation", "first_date": None, "last_date": None, "observed_at": CUTOFF},
    ]
    # 2026-08-14 is in the protected 79-calendar-day interval; mixed dates cannot be spliced.
    materials[DATES]["eligible_test_dates"] += ["2026-08-14"]
    result = audit(tmp_path, seal(tmp_path, root, materials, sources))
    c = codes(result)
    assert c[EXPOSURE, "eligible_date_has_prior_exposure"] == 2
    assert c[EXPOSURE, "exposure_dates_unknown"] == 1
    assert c[DATES, "quarantined_test_date"] == 1
    assert c[DATES, "noncontiguous_eligible_dates"] == 1


def _day_settlement():
    _, material, docs = make()
    d = day("2024-03-17")
    symbol = PERPS[0]
    one = material[FUNDING]
    source = {name: (doc["kind"], instant(doc["captured_at"])) for name, doc in docs.items()}
    body = {
        "cadence": [r for r in one["cadence"] if r["instrument"] == symbol],
        **{
            k: [r for r in one[k] if r["instrument"] == symbol and day(r["scheduled_at"][:10]) == d]
            for k in ("marks", "rates")
        },
    }
    return body, {symbol: {d}}, source


def test_missing_both_fails_and_mismatched_paired_actual_time_fails():
    body, due, source = _day_settlement()
    body["rates"].pop()
    body["marks"].pop()
    actual = instant(body["marks"][0]["settled_at"]) + timedelta(seconds=1)
    body["marks"][0]["settled_at"] = actual.isoformat().replace("+00:00", "Z")
    findings = Findings()
    result = settlement(body, due, source, instant(CUTOFF), findings, FUNDING)
    assert result == {"expected": 3, "paired": 1}
    counts = codes({"findings": findings.result()})
    assert counts[FUNDING, "missing_settled_rate"] == counts[FUNDING, "missing_exact_settlement_mark"] == 1
    assert counts[FUNDING, "settlement_event_id_or_actual_time_mismatch"] == 1


@pytest.mark.parametrize(
    ("field", "label", "kind", "reason"),
    [
        ("marks", "mark_kind", "1m_ohlc", "mark_is_not_exact_settlement_price"),
        ("rates", "rate_kind", "current_ws_snapshot", "rate_is_not_actual_settled_rate"),
    ],
)
def test_cannot_substitute_proxy_or_ws(field, label, kind, reason):
    body, due, source = _day_settlement()
    body[field][0][label] = kind
    with pytest.raises(Invalid, match=reason):
        settlement(body, due, source, instant(CUTOFF), Findings(), FUNDING)


def test_discontinuous_cadence_and_duplicate_marks_are_detected():
    body, due, source = _day_settlement()
    body["cadence"][1]["start_utc"] = "2024-03-17T01:00:00Z"
    body["marks"].append(dict(body["marks"][0]))
    f = Findings()
    assert settlement(body, due, source, instant(CUTOFF), f, FUNDING)["expected"] == 3
    counts = codes({"findings": f.result()})
    assert counts[FUNDING, "duplicate_settlement_key"] == 1
    assert counts[FUNDING, "missing_settlement_cadence_coverage"] == 1


def test_self_review_and_fake_policy_fail(tmp_path):
    root, materials, sources = make()
    materials[FREEZE]["configuration_ids"] += ["seventh"]
    receipt = {"reviewer_id": root["prepared_by"], "source_id": "independent_review_receipt"}
    result = audit(tmp_path, seal(tmp_path, root, materials, sources, receipt=receipt))
    assert codes(result)[APPROVAL, "reviewer_not_distinct_from_preparer"] == 1
    assert codes(result)[FREEZE, "six_configuration_budget_mismatch"] == 1
    assert result["real_execution_allowed"] is False


def test_reserve_requires_original_window_disposition_and_completed_dates():
    import json
    from pathlib import Path

    from research.evidence.calendar import calendar

    registration = json.loads(
        (Path(__file__).resolve().parents[1] / "research/experiments/ACCEPTANCE_R1.json").read_bytes()
    )
    src = {"eligible_calendar": ("eligible_calendar", instant(CUTOFF))}
    data = {
        "source_id": "eligible_calendar",
        "test_window": "future_reserve_if_original_test_invalid",
        "eligible_test_dates": ["2026-11-21"],
    }
    with pytest.raises(Invalid, match="reserve_requires_original_test_disposition"):
        calendar(data, registration, instant(CUTOFF), src, Findings(), DATES)
    with pytest.raises(Invalid, match="eligible_date_not_complete_at_audit_cutoff"):
        calendar(
            data | {"original_test_disposition": "invalid_after_independent_audit"},
            registration,
            instant(CUTOFF),
            src,
            Findings(),
            DATES,
        )


def test_upstream_report_source_freeze_still_verifies():
    from research.reporting.report import verify_reporting_source

    assert verify_reporting_source() == "ca894d5434ff37a63b6e415b0f666667d8ee94ba38c29aa0f66839d8e4cb0400"

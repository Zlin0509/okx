import gzip
import json
from decimal import Decimal

import pytest
from test_research_engine import market

from research.data_audit import DAY, stamp
from research.eligibility import add_candles, evaluate_candidate, liquidity_window
from research.engine import Costs, Engine, Leg, Rejected, Signal
from research.execution_audit import canonical, counts, read_log, run_logged, sha
from research.m2 import integrate_once, real_integration, real_window_inputs, require_controlled_snapshot
from research.verify_m2 import artifact_digest, check_manifest


def volume_days(value="10000000"):
    day = stamp("2021-01-01")
    return day, {day - lag * DAY: value for lag in range(1, 31)}


@pytest.mark.parametrize(
    ("value", "passed"),
    [("0", False), ("1", False), ("9999999.99", False), ("10000000", True), ("20000000", True)],
)
def test_lagged_turnover_numerical_floor_and_inclusive_boundary(value, passed):
    day, values = volume_days(value)
    result = liquidity_window(day, values)
    assert result["passed"] == passed
    assert Decimal(result["mean_daily_usdt"]) == Decimal(value)
    assert result["valid_observations"] == 30


@pytest.mark.parametrize("bad", [None, "", "NaN", "Infinity", "-1"])
def test_missing_nonfinite_or_negative_turnover_never_zero_filled(bad):
    day, values = volume_days()
    values[day - DAY] = bad
    result = liquidity_window(day, values)
    assert not result["passed"] and result["mean_daily_usdt"] is None
    assert result["valid_observations"] == 29


def test_zero_is_valid_but_current_future_and_day_minus_31_are_excluded():
    day, values = volume_days("20000000")
    values[day - DAY] = "0"
    original = liquidity_window(day, values)
    values.update({day: "NaN", day + DAY: "999999999999", day - 31 * DAY: "0"})
    assert liquidity_window(day, values) == original
    assert original["passed"] and original["valid_observations"] == 30
    values[day - 30 * DAY] = ""
    assert not liquidity_window(day, values)["passed"]


def test_actual_quote_turnover_column_not_contract_or_base_volume():
    prices, turnover = {}, {}
    add_candles(
        [["0", "100", "101", "99", "100", "99999999999", "99999999999", "0", "1"]], prices, turnover, "A"
    )
    assert turnover == {"A": {0: "0"}}
    with pytest.raises(ValueError, match="Conflicting"):
        add_candles([["0", "100", "101", "99", "100", "1", "1", "2", "1"]], prices, turnover, "A")


def test_unverified_tradability_stays_unknown_even_when_numerical_liquidity_passes():
    day, values = volume_days()
    prices = {"A": set(range(day - 400 * DAY, day + DAY, DAY))}
    meta = {"A": {"listing": day - 400 * DAY, "continuous": day - 400 * DAY, "verified": False}}
    row = evaluate_candidate(["A"], prices, meta, [day], turnover={"A": values})[0]
    assert row["verified_status"] == "unknown" and row["provisional_current_metadata_pass"]
    low = evaluate_candidate(["A"], prices, meta, [day], turnover={"A": {t: "0" for t in values}})[0]
    assert low["verified_status"] == "unknown" and not low["provisional_current_metadata_pass"]


def test_append_log_records_utc_head_sources_results_and_counts_from_entries(tmp_path):
    path = tmp_path / "runs.jsonl"
    expected = {"case": "synthetic-only", "answer": "42"}
    assert run_logged("unit-test", lambda: expected, path) == expected
    entries = read_log(path)
    assert counts(entries)["attempts"] == counts(entries)["completed"] == 1
    assert entries[0]["started_at_utc"].endswith("+00:00")
    assert len(entries[0]["HEAD"]) == 40
    assert "verify_m2.py" in entries[0]["source_sha256"]
    assert entries[1]["finished_at_utc"] >= entries[0]["started_at_utc"]
    assert entries[1]["result_sha256"] == sha(canonical(expected))
    with pytest.raises(ZeroDivisionError):
        run_logged("failed-test", lambda: 1 / 0, path)
    assert counts(read_log(path))["failed"] == 1
    changed = path.read_text().replace("unit-test", "edited-test")
    path.write_text(changed)
    with pytest.raises(ValueError, match="hash mismatch"):
        read_log(path)


def test_gzip_manifest_protects_plaintext_while_accepting_header_variants(tmp_path):
    path = tmp_path / "rows.csv.gz"
    path.write_bytes(gzip.compress(b"a,b\n1,2\n", mtime=0))
    manifest = {path.name: artifact_digest(path)}
    other = bytearray(path.read_bytes())
    other[9] = 255 if other[9] != 255 else 3
    path.write_bytes(other)
    check_manifest(tmp_path, manifest)
    path.write_bytes(gzip.compress(b"a,b\n1,3\n", mtime=0))
    with pytest.raises(ValueError, match="checksum mismatch"):
        check_manifest(tmp_path, manifest)


@pytest.mark.parametrize("entry", [real_integration, real_window_inputs, integrate_once])
def test_real_window_entry_is_disabled_without_accessing_any_snapshot(entry, monkeypatch):
    monkeypatch.setattr("research.m2.verify", lambda *_: pytest.fail("Must not inspect real window"))
    with pytest.raises(Rejected) as error:
        entry()
    assert error.value.code == "real_window_disabled"


def test_code_only_controlled_entry_rejects_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr("research.m2.DEFAULT", tmp_path)
    with pytest.raises(Rejected) as error:
        require_controlled_snapshot()
    assert error.value.code == "controlled_snapshot_required"


def test_risk_halt_counts_actual_pending_and_preloaded_future_intents():
    data = market([{"A": a, "B": 100} for a in (100, 100, 1000, 900, 900)])
    result = Engine(data, costs=Costs(Decimal(0), Decimal(0))).run(
        [
            Signal("open", 0, {Leg("pair", "A"): Decimal(20)}),
            Signal("future", 2500, {Leg("pair", "B"): Decimal(-1)}),
        ]
    )
    halt = next(row for row in result["ledger"] if row["event"] == "risk_halt")
    assert halt["cancelled_pending"] == 1
    assert halt["cancelled_known_pending"] == 0
    assert halt["cancelled_preloaded_future_signals"] == 1
    assert not [r for r in result["ledger"] if r["event"] == "fill" and r["symbol"] == "B"]


def test_both_bases_are_explicit_in_r2_protocol():
    from pathlib import Path

    protocol = json.loads((Path(__file__).parent.parent / "research/M2_PROTOCOL_R2.json").read_text())
    assert protocol["controlled_base"].startswith("c5f6ffc")
    assert protocol["code_base"].startswith("d67cfd2")
    assert "base_commit" not in protocol

import pytest

from research.data_audit import DAY, PublicCollector, collect, freeze, stamp
from research.eligibility import evaluate_candidate


def volumes(prices):
    return {s: {t: "10000000" for t in times} for s, times in prices.items()}


def test_verified_listing_complete_first_day_180_day_gate_and_rolling_warmup():
    first = stamp("2020-01-01")
    # A noon listing is not a complete day. First full day is Jan 2, then 180+56 full days.
    meta = {"A": {"listing": first + DAY // 2, "continuous": first + DAY // 2, "verified": True}}
    eligible = first + (1 + 180 + 56) * DAY
    prices = {"A": set(range(first, eligible + DAY, DAY))}
    rows = evaluate_candidate(["A"], prices, meta, [eligible - DAY, eligible], turnover=volumes(prices))
    assert rows[0]["verified_status"] == "excluded"
    assert "A:rolling_post_admission_warmup" in rows[0]["provisional_exclusion_reasons"]
    assert rows[1]["verified_status"] == "eligible"


def test_unknown_listing_is_not_replaced_by_first_observed_price():
    first = stamp("2020-01-01")
    prices = {"A": set(range(first, first + 400 * DAY, DAY))}
    row = evaluate_candidate(["A"], prices, {}, [first + 300 * DAY], turnover=volumes(prices))[0]
    assert row["verified_status"] == "unknown"
    assert "A:listing_time_unknown" in row["unknown_reasons"]
    assert not row["provisional_current_metadata_pass"]


def test_current_unverified_metadata_never_becomes_statistical_eligibility():
    first = stamp("2020-01-01")
    meta = {"A": {"listing": first, "continuous": first, "verified": False}}
    prices = {"A": set(range(first, first + 400 * DAY, DAY))}
    row = evaluate_candidate(["A"], prices, meta, [first + 300 * DAY], turnover=volumes(prices))[0]
    assert row["provisional_current_metadata_pass"]
    assert row["verified_status"] == "unknown"


def test_common_leg_gap_and_later_tradability_exclude_exact_dates():
    first = stamp("2020-01-01")
    meta = {s: {"listing": first, "continuous": first, "verified": True} for s in ("A", "B")}
    prices = {s: set(range(first, first + 400 * DAY, DAY)) for s in meta}
    day = first + 300 * DAY
    prices["B"].remove(day - DAY)
    row = evaluate_candidate(["A", "B"], prices, meta, [day], turnover=volumes(prices))[0]
    assert row["verified_status"] == "excluded"
    assert "B:missing_lagged_price_window" in row["provisional_exclusion_reasons"]
    assert "B:missing_lagged_turnover_window" in row["provisional_exclusion_reasons"]
    meta["A"]["continuous"] = day
    row = evaluate_candidate(["A"], prices, meta, [day], turnover=volumes(prices))[0]
    assert "A:listing_age_180_not_met" in row["provisional_exclusion_reasons"]


def test_frozen_directory_cannot_collect_or_refreeze(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr("urllib.request.build_opener", lambda: pytest.fail("No network"))
    for operation in (lambda: PublicCollector(tmp_path), lambda: collect(tmp_path), lambda: freeze(tmp_path)):
        with pytest.raises(ValueError, match="read-only"):
            operation()
    assert (tmp_path / "manifest.json").read_text() == "{}"


def test_full_funding_page_explicitly_refuses_completeness_without_retry(tmp_path, monkeypatch):
    calls = []

    class FakeCollector:
        def __init__(self, root):
            pass

        def get(self, path, params):
            calls.append(path)
            if path.endswith("/history-candles"):
                return [[str(stamp("2020-07-01"))]]
            return [{}] * 400 if path.endswith("/funding-rate-history") else []

    monkeypatch.setattr("research.data_audit.PublicCollector", FakeCollector)
    monkeypatch.setattr("research.data_audit.PROBES", ("A", "B"))
    with pytest.raises(RuntimeError, match="truncated"):
        collect(tmp_path)
    assert calls.count("/api/v5/public/funding-rate-history") == 1

import json
import urllib.error
from unittest.mock import Mock

import pytest

from research.data_audit import (
    DAY,
    HOST,
    PublicCollector,
    candle_summary,
    common_summary,
    digest,
    dump,
    freeze,
    records,
    stamp,
    verify,
)


def candle(ts, **changes):
    row = [str(ts), "10", "12", "9", "11", "1", "1", "10", "1"]
    for index, value in changes.items():
        row[int(index)] = value
    return row


def snapshot(root, body, status=200, path="/api/v5/public/funding-rate-history"):
    payload = json.dumps(body).encode()
    (root / "test.body").write_bytes(payload)
    dump(
        root / "test.request.json",
        {
            "url": HOST + path + "?instId=BTC-USDT-SWAP&limit=400",
            "method": "GET",
            "retrieved_at": "2026-11-13T00:00:00+00:00",
            "status": status,
            "bytes": len(payload),
            "sha256": digest(payload),
        },
    )


def test_daily_coverage_distinguishes_internal_gaps_and_end_exclusive():
    start = stamp("2021-01-01")
    result = candle_summary(
        [candle(start + DAY), candle(start + 3 * DAY), candle(start + 4 * DAY)], start, start + 4 * DAY
    )
    assert result["rows"] == 2
    assert result["missing_calendar_days"] == 2
    assert result["internal_missing_dates"] == ["2021-01-03T00:00:00Z"]
    assert result["last"] == "2021-01-04T00:00:00Z"


@pytest.mark.parametrize(
    "row",
    [
        candle(1),
        candle(0, **{"8": "0"}),
        candle(0, **{"1": "NaN"}),
        candle(0, **{"3": "13"}),
        candle(0, **{"5": "-1"}),
        candle(0, **{"2": "10"}),
    ],
)
def test_invalid_candles_rejected(row):
    with pytest.raises(ValueError):
        candle_summary([row], 0, DAY)


def test_duplicate_overlap_is_counted_but_revision_is_rejected():
    assert candle_summary([candle(0), candle(0)], 0, DAY)["duplicate_rows_across_probes"] == 1
    with pytest.raises(ValueError, match="Conflicting"):
        candle_summary([candle(0), candle(0, **{"4": "10"})], 0, DAY)


def test_cross_asset_alignment_retains_holes_and_uses_intersection():
    result = common_summary([{0, DAY, 2 * DAY, 3 * DAY}, {DAY, 3 * DAY, 4 * DAY}], DAY)
    assert result["rows"] == 2
    assert result["first"] == "1970-01-02T00:00:00Z"
    assert result["missing_in_common_span"] == ["1970-01-03T00:00:00Z"]
    assert common_summary([], DAY)["first"] is None


def test_manifest_and_coverage_reproduce_without_network(tmp_path, monkeypatch):
    snapshot(tmp_path, {"code": "0", "data": []})
    monkeypatch.setattr("urllib.request.build_opener", Mock(side_effect=AssertionError("No network")))
    freeze(tmp_path)
    verify(tmp_path)
    result = json.loads((tmp_path / "coverage.json").read_text())
    assert result["ready_for_full_period_costed_backtest"] is False
    assert result["historical_universe_complete"] is False
    (tmp_path / "unregistered.body").write_bytes(b"extra")
    with pytest.raises(ValueError, match="Manifest"):
        verify(tmp_path)


def test_changed_raw_response_fails_checksum(tmp_path):
    snapshot(tmp_path, {"code": "0", "data": []})
    (tmp_path / "test.body").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        records(tmp_path)


@pytest.mark.parametrize("status", [403, 429])
def test_http_failure_persisted_and_not_retried(tmp_path, monkeypatch, status):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError(HOST, status, "blocked", {}, None)
    monkeypatch.setattr("urllib.request.build_opener", Mock(return_value=opener))
    monkeypatch.setattr("time.sleep", lambda _: None)
    collector = PublicCollector(tmp_path)
    params = {"instId": "BTC-USDT-SWAP", "limit": "400"}
    with pytest.raises(RuntimeError):
        collector.get("/api/v5/public/funding-rate-history", params)
    # Restarting collection must not retry a blocked path, even with a new symbol.
    collector = PublicCollector(tmp_path)
    with pytest.raises(RuntimeError):
        collector.get("/api/v5/public/funding-rate-history", {**params, "instId": "ETH-USDT-SWAP"})
    assert opener.open.call_count == 1
    assert records(tmp_path)[0][0]["status"] == status


def test_nonpublic_endpoint_rejected_before_network(tmp_path):
    with pytest.raises(ValueError, match="allowlist"):
        PublicCollector(tmp_path).get("/api/v5/trade/order", {})


@pytest.mark.parametrize("code", ["50011", "50040"])
def test_provider_quota_stops_all_endpoints_including_after_restart(tmp_path, monkeypatch, code):
    snapshot(tmp_path, {"code": code, "data": []})
    monkeypatch.setattr("urllib.request.build_opener", Mock(side_effect=AssertionError("No retry")))
    with pytest.raises(RuntimeError, match="Quota"):
        PublicCollector(tmp_path).get("/api/v5/public/instruments", {"instType": "SWAP"})


def test_missing_realized_funding_never_replaced_by_predicted_rate(tmp_path):
    snapshot(
        tmp_path,
        {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "fundingTime": "1790812800000",
                    "realizedRate": "",
                    "fundingRate": "0.0001",
                }
            ],
        },
    )
    with pytest.raises(ValueError):
        freeze(tmp_path)

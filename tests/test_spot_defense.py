import copy
import json
from datetime import date, timedelta

import pytest

from research.spot_defense.collect import COINBASE, CUTOFF_MS, OKX, coinbase_rows, okx_rows, sha256
from research.spot_defense.data import FX, Bar, Interruption, Market, load
from research.spot_defense.engine import decision, metrics, series_metrics, simulate
from research.spot_defense.protocol import DAY, LOCK_START, SYMBOLS, WITNESS_DAY, at, days
from research.spot_defense.stress import SCENARIOS, run_stress

MONDAY = date(2024, 1, 1)
BTC, ETH = SYMBOLS


def rising():
    market = Market(listed=frozenset(SYMBOLS))
    for offset, day in enumerate(days(MONDAY - 60 * DAY, MONDAY + 10 * DAY)):
        for symbol in SYMBOLS:
            market.daily[symbol][day] = Bar(40 + offset, 20_000_000, at(day + DAY))
            market.opens[symbol][day] = 100
        market.daily[FX][day] = Bar(1, 1, at(day + DAY))
    return market


def simulate_day(market, **kwargs):
    return simulate(market, MONDAY, MONDAY + DAY, 20, **kwargs)[0][0]


def test_future_candle_and_window_changes_do_not_change_past_or_same_day_check():
    market = rising()
    baseline = simulate(market, MONDAY, MONDAY + 5 * DAY, 20)[0]
    modified = copy.deepcopy(market)
    day = MONDAY + 3 * DAY
    modified.daily[BTC][day] = Bar(5, 1, at(day + DAY))
    modified.daily[FX][day] = Bar(0.8, 1, at(day + DAY))
    modified.opens[ETH][day] = 2_000
    observed = simulate(modified, MONDAY, MONDAY + 5 * DAY, 20)[0]
    assert observed[:3] == baseline[:3]
    for key in ("signals", "check_add_candidates", "check_exit_intents", "fx_at_check"):
        assert observed[3][key] == baseline[3][key]
    assert observed[4]["signals"] != baseline[4]["signals"]


def test_completed_bar_late_for_0100_and_missing_fx_block_additions():
    market = rising()
    prior = MONDAY - DAY
    market.daily[BTC][prior] = Bar(900, 50_000_000, at(MONDAY, 1) + timedelta(seconds=1))
    assert not decision(market, MONDAY)[0][ETH]
    assert simulate_day(market)["fills"] == []
    market = rising()
    del market.daily[FX][prior]
    row = simulate_day(market)
    assert FX in row["add_blocks"]
    assert row["fx_at_check"] == 1
    assert row["fills"] == []


@pytest.mark.parametrize("fx", [0.98, 1.02, 0.90])
def test_depeg_boundary_blocks_buys_but_not_sales(fx):
    market = rising()
    market.daily[FX][MONDAY - DAY] = Bar(fx, 1, at(MONDAY))
    assert simulate_day(market)["fills"] == []
    market.daily[BTC][MONDAY - DAY] = Bar(50, 20_000_000, at(MONDAY))
    row = simulate_day(market, initial_positions={BTC: 12.5, ETH: 12.5})
    assert "usdt_depeg" in row["add_blocks"]
    assert [fill["side"] for fill in row["fills"]] == ["sell", "sell"]


def test_strict_trend_listing_and_median_volume():
    market = rising()
    market.listed = frozenset({BTC})
    assert [fill["symbol"] for fill in simulate_day(market)["fills"]] == [BTC]
    for day in days(MONDAY - 30 * DAY, MONDAY):
        bar = market.daily[BTC][day]
        market.daily[BTC][day] = Bar(bar.close, 9_999_999, bar.available)
    assert simulate_day(market)["fills"] == []
    market = rising()
    for day in days(MONDAY - 60 * DAY, MONDAY):
        market.daily[BTC][day] = Bar(100, 20_000_000, at(day + DAY))
    assert simulate_day(market)["check_add_candidates"] == []


def test_no_fill_during_halt_retries_exit_even_after_signal_recovers():
    market = rising()
    market.daily[BTC][MONDAY - DAY] = Bar(5, 20_000_000, at(MONDAY))
    market.interruptions = (Interruption(at(MONDAY, 2), at(MONDAY, 3), at(MONDAY, 2), (BTC,)),)
    ledger = simulate(market, MONDAY, MONDAY + 2 * DAY, 20, initial_positions={BTC: 12.5, ETH: 12.5})[0]
    assert not ledger[0]["signals"][BTC]["known_interruption"]
    assert ledger[0]["execution_open"][BTC] is None
    assert ledger[0]["pending_exits"][BTC]["target"] == 0
    assert [fill["symbol"] for fill in ledger[0]["fills"]] == [ETH]
    assert ledger[1]["signals"][BTC]["trend"]
    assert ledger[1]["exit_completions"] == [{"symbol": BTC, "days": 1, "target": 0.0}]


def test_missing_monday_entry_expires_and_passive_waits_for_shared_open():
    market = rising()
    del market.opens[ETH][MONDAY]
    for passive in (False, True):
        ledger = simulate(market, MONDAY, MONDAY + 3 * DAY, 20, passive=passive)[0]
        assert ledger[0]["fills"] == [] and ledger[0]["rejected"]
        assert len(ledger[1]["fills"]) == (2 if passive else 0)
        assert ledger[2]["fills"] == []
        assert all(row["cash_usdt"] >= 0 for row in ledger)


def test_pricing_cost_fee_turnover_and_caps_net_of_cost():
    market = rising()
    row = simulate_day(market)
    assert row["post_total_weight"] == pytest.approx(0.25)
    for fill in row["fills"]:
        assert fill["base_price"] == 100
        assert fill["fill_price"] == pytest.approx(100.1)
        assert fill["fee_usdt"] == pytest.approx(fill["notional_usdt"] / 1000)
    assert row["turnover"] == pytest.approx(sum(fill["notional_usdt"] for fill in row["fills"]) / 10_000)
    market.opens[BTC][MONDAY] = 150
    row = simulate_day(market, initial_positions={BTC: 12.5, ETH: 12.5})
    assert [fill["side"] for fill in row["fills"]] == ["sell"]
    assert row["post_weights"][BTC] == pytest.approx(0.125)
    assert row["post_total_weight"] <= 0.30


def test_risk_cap_rechecked_when_other_leg_exits_and_cost_lowers_nav():
    market = rising()
    market.daily[ETH][MONDAY - DAY] = Bar(20, 20_000_000, at(MONDAY))
    row = simulate_day(market, initial_positions={BTC: 15, ETH: 10})
    assert [fill["symbol"] for fill in row["fills"]] == [ETH, BTC]
    assert row["post_weights"][BTC] == pytest.approx(0.125)
    assert not row["post_exposure_breach"]


def test_metrics_include_initial_nav_and_do_not_certify_stale_quotes():
    assert series_metrics([100, 90] + [93] * 29)["worst_rolling_30d"] == pytest.approx(-0.07)
    assert series_metrics([100, 90])["max_drawdown"] == pytest.approx(0.10)
    market = rising()
    del market.opens[ETH][MONDAY]
    del market.daily[ETH][MONDAY]
    ledger, initial_usd = simulate(market, MONDAY, MONDAY + 4 * DAY, 20)
    result = metrics(ledger, initial_usd, assess=True)
    assert result["stale_valuation_days"] == result["stale_execution_days"] == 1
    assert result["acceptance"] == "insufficient_evidence"


def test_all_stresses_protect_cash_and_retain_missing_exit_intents():
    results = run_stress()
    assert tuple(results) == SCENARIOS
    assert all(all(result["protections"].values()) for result in results.values())
    assert results["depeg_seven_days"]["worst_truth_drawdown"] == pytest.approx(0.10)
    assert results["combined"]["worst_truth_drawdown"] > 0.19
    for key in ("missing_three_windows", "combined"):
        result = results[key]
        assert result["max_exit_delay_days"] == 3
        assert result["rejected_sell_attempts"] == 6
        assert all(row["fills"] == [] for row in result["ledger"][:3])
        assert all(fill["base_price"] == 60 for fill in result["ledger"][3]["fills"])


def test_raw_market_rejects_locked_rows_before_saving():
    with pytest.raises(ValueError, match="boundary"):
        okx_rows(json.dumps({"code": "0", "data": [[CUTOFF_MS, *[0] * 8]]}).encode(), CUTOFF_MS)
    with pytest.raises(ValueError, match="boundary"):
        coinbase_rows(json.dumps([[CUTOFF_MS // 1000, *[0] * 5]]).encode(), CUTOFF_MS // 1000)


def _fixture(tmp_path, body, series, url, **extra):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "data.json").write_bytes(body)
    manifest = json.dumps(
        {
            "schema": 1,
            "timezone": "UTC",
            "first_requested_date": str(WITNESS_DAY),
            "end_exclusive": str(LOCK_START),
            "entries": [
                {
                    "file": "raw/data.json",
                    "series": series,
                    "url": url,
                    "bytes": len(body),
                    "sha256": sha256(body),
                    **extra,
                }
            ],
        }
    ).encode()
    (tmp_path / "manifest.json").write_bytes(manifest)
    return sha256(manifest)


def test_load_checks_trusted_root_raw_hash_and_hour_trade_volume(tmp_path):
    stamp = int(at(MONDAY, 2).timestamp()) * 1000
    body = json.dumps(
        {"code": "0", "data": [[stamp, "100", "100", "100", "100", "0", "0", "0", "1"]]}
    ).encode()
    url = f"{OKX}?instId={BTC}&bar=1H&after={CUTOFF_MS}"
    checksum = _fixture(tmp_path, body, f"{BTC}:1H", url, cursor=CUTOFF_MS)
    with pytest.raises(ValueError, match="manifest SHA-256"):
        load(tmp_path, "bad")
    market, _ = load(tmp_path, checksum)
    assert market.execution_price(BTC, MONDAY) is None
    (tmp_path / "raw/data.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="raw response mismatch"):
        load(tmp_path, checksum)


def test_load_coinbase_ignores_documented_earlier_spill(tmp_path):
    start = at(MONDAY)
    end = at(MONDAY + DAY) - timedelta(seconds=1)
    lower, upper = int(start.timestamp()), int(end.timestamp())
    params = "granularity=86400&start=2024-01-01T00%3A00%3A00Z&end=2024-01-01T23%3A59%3A59Z"
    body = json.dumps([[lower - 86400, *[1] * 5], [lower, *[1] * 5]]).encode()
    checksum = _fixture(tmp_path, body, FX, f"{COINBASE}?{params}", lower=lower, upper=upper)
    market, _ = load(tmp_path, checksum)
    assert set(market.daily[FX]) == {MONDAY}

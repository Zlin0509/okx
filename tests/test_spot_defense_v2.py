import copy
import json
import subprocess
import sys
from datetime import date, timedelta

import pytest

from research.spot_defense.data import FX
from research.spot_defense.engine import Portfolio
from research.spot_defense.protocol import DAY, SYMBOLS, at, days
from research.spot_defense.stress import run_stress
from research.spot_defense.v2 import gate
from research.spot_defense.v2.calendar import (
    END,
    EVAL_START,
    INITIAL_RESERVE_SHA256,
    PREREG_SHA256,
    V1_CONFIG_SHA256,
    WARMUP_START,
    bounds,
)
from research.spot_defense.v2.evidence import (
    AVAILABILITY,
    FILES,
    LIVE,
    STATE_SOURCE,
    TRADE_SOURCE,
    _candle,
    _status,
    _trade,
    digest,
    inspect_manifest,
    read_evidence,
)
from research.spot_defense.v2.replay import simulate, synthetic_replay
from research.spot_defense.v2.run import evaluate, review

BTC, ETH = SYMBOLS
DAY0 = EVAL_START  # Monday
AUDIT_HASH = "a" * 64
KINDS = ("daily", "check_state", "execution_state", "trade_feed", "first_trade")


def _z(instant):
    return instant.isoformat().replace("+00:00", "Z")


def _candle_row(day, symbol, close, *, delay=3599, confirm="1"):
    stamp = int(at(day).timestamp())
    candle = (
        [stamp, close, close, close, close, 1]
        if symbol == FX
        else [str(stamp * 1000), close, close, close, close, 5, 5, 20_000_000, confirm]
    )
    return {
        "symbol": symbol,
        "day": str(day),
        "source": AVAILABILITY[symbol],
        "received_at": _z(at(day + DAY) + timedelta(seconds=delay)),
        "response_sha256": AUDIT_HASH,
        "candle": candle,
    }


def _status_row(day, symbol, kind, state=None, *, start=None, end=None):
    if kind == "check":
        start = at(day, 1) - timedelta(seconds=1) if start is None else start
        end = at(day, 1)
    else:
        start = at(day, 2) if start is None else start
        end = at(day, 3) if end is None else end
    return {
        "day": str(day),
        "symbol": symbol,
        "kind": kind,
        "source": TRADE_SOURCE if kind == "feed" else STATE_SOURCE[symbol],
        "start": _z(start),
        "end": _z(end),
        "state": state or (symbol if kind == "feed" else LIVE[symbol]),
        "response_sha256": AUDIT_HASH,
    }


def _trade_row(day, symbol, price, *, offset=1, receipt=None, label=""):
    event = at(day, 2) + timedelta(seconds=offset)
    received = at(day, 2) + timedelta(seconds=receipt) if receipt is not None else event
    tick = {
        "instId": symbol,
        "tradeId": f"{day}-{symbol}-{offset}{label}",
        "ts": str(int(event.timestamp() * 1000)),
        "px": str(price),
        "sz": "0.01",
        "side": "buy",
        "source": "0",
    }
    return {
        "day": str(day),
        "symbol": symbol,
        "source": TRADE_SOURCE,
        "received_at": _z(received),
        "stream": symbol,
        "response_sha256": AUDIT_HASH,
        "frame": {"arg": {"channel": "trades-all", "instId": symbol}, "data": [tick]},
    }


def _inputs(end=DAY0 + DAY):
    candles, states, trades = [], [], []
    for offset, day in enumerate(days(WARMUP_START, end)):
        for symbol in AVAILABILITY:
            candles.append(_candle_row(day, symbol, 1 if symbol == FX else 40 + offset))
    for day in days(EVAL_START, end):
        for symbol in AVAILABILITY:
            states.extend(_status_row(day, symbol, kind) for kind in ("check", "state"))
        for index, symbol in enumerate(SYMBOLS):
            states.append(_status_row(day, symbol, "feed"))
            trades.append(_trade_row(day, symbol, 100, offset=index + 1))
    return {"availability.json": candles, "status.json": states, "trades.json": trades}


def _pack(root, inputs, *, counts=None, receipts=True):
    files = copy.deepcopy(inputs)
    receipts_from_rows = {
        (row["source"], row.get("received_at", row.get("start")), row["response_sha256"])
        for rows in files.values()
        for row in rows
    }
    files["provenance.json"] = (
        [
            {"source": source, "started_at": received, "response_sha256": sha}
            for source, received, sha in sorted(receipts_from_rows)
        ]
        if receipts
        else []
    )
    manifest = {
        "schema": "zlin21-v2-evidence-1",
        "bounds": bounds(),
        "files": {},
        "source_manifest_sha256": AUDIT_HASH,
        "missing_counts": counts or dict.fromkeys(KINDS, 0),
    }
    assert set(files) == FILES
    root.mkdir(exist_ok=True)
    for name, rows in files.items():
        body = gate.report_bytes(rows)
        (root / name).write_bytes(body)
        manifest["files"][name] = {"bytes": len(body), "records": len(rows), "sha256": digest(body)}
    body = gate.report_bytes(manifest)
    (root / "manifest.json").write_bytes(body)
    return body, inspect_manifest(body, expected_hash=digest(body))


def _read(tmp_path, inputs):
    _, manifest = _pack(tmp_path, inputs)
    return read_evidence(tmp_path, manifest)


def _row(market, **kw):
    return simulate(market, 20, start=DAY0, end=DAY0 + DAY, **kw)[0][0]


@pytest.mark.parametrize("date_text", ["2025-07-01", "2026-10-31", "2026-12-02", "2028-02-01"])
def test_both_bounds_and_permanent_window(date_text):
    day = date.fromisoformat(date_text)
    for row in (_candle_row(day, BTC, 100), _candle_row(day, FX, 1)):
        with pytest.raises(ValueError, match="sealed v1|outside frozen"):
            _candle(row)
    with pytest.raises(ValueError, match="sealed v1|outside frozen"):
        _trade(_trade_row(day, BTC, 100))


def test_payload_timestamps_cannot_hide_sealed_data_and_warmup_is_daily_only():
    row = _candle_row(WARMUP_START, BTC, 100, confirm="0")
    row["candle"][0] = str(int(at(WARMUP_START - 34 * DAY).timestamp() * 1000))
    with pytest.raises(ValueError, match="sealed v1"):
        _candle(row)
    with pytest.raises(ValueError, match="outside frozen"):
        _trade(_trade_row(WARMUP_START, BTC, 100))
    with pytest.raises(ValueError, match="outside causal execution"):
        _trade(_trade_row(DAY0, BTC, 100, receipt=3600))
    with pytest.raises(ValueError, match="outside causal execution"):
        _trade(_trade_row(DAY0, BTC, 100, offset=-1))
    assert _candle(_candle_row(WARMUP_START, FX, 1))[1] == WARMUP_START
    assert _candle(_candle_row(END - DAY, BTC, 100))[2].available.date() == END


@pytest.mark.parametrize(
    "delay,confirm,eligible", [(3599, "1", True), (3600, "1", False), (3601, "1", False), (3599, "0", False)]
)
def test_only_complete_bars_received_strictly_before_0100(tmp_path, delay, confirm, eligible):
    rows = _inputs()
    rows["availability.json"] = [
        r for r in rows["availability.json"] if (r["day"], r["symbol"]) != (str(DAY0 - DAY), BTC)
    ]
    rows["availability.json"].append(_candle_row(DAY0 - DAY, BTC, 99, delay=delay, confirm=confirm))
    market = _read(tmp_path, rows)
    first = _row(market)
    assert bool(first["fills"]) == eligible
    assert (BTC in first["checked_stale"]) != eligible
    if not eligible:
        assert market.bar(BTC, DAY0 - DAY, at(DAY0 + DAY, 1)) is None


def test_earliest_completed_receipt_wins_over_revised_candle(tmp_path):
    rows = _inputs()
    previous = DAY0 - DAY
    rows["availability.json"].extend(
        [
            _candle_row(previous, ETH, 5, delay=2000),
            _candle_row(previous, ETH, 1000, delay=2500),
        ]
    )
    market = _read(tmp_path, rows)
    assert _row(market)["signals"][ETH]["latest_close"] == 5


def test_earlier_fill_is_independent_of_later_peer_price_or_trade_presence(tmp_path):
    rows = _inputs()
    before = _row(_read(tmp_path, rows))["fills"]
    assert [fill["symbol"] for fill in before] == list(SYMBOLS)
    assert [fill["trade_id"] for fill in before] == [f"{DAY0}-{BTC}-1", f"{DAY0}-{ETH}-2"]
    rows["trades.json"][1] = _trade_row(DAY0, ETH, 1_000_000, offset=9)
    assert before[0] == _row(_read(tmp_path, rows))["fills"][0]
    rows["trades.json"].pop()
    assert before[0] == _row(_read(tmp_path, rows))["fills"][0]


def test_earlier_buy_cannot_use_late_mark_for_existing_peer_holding(tmp_path):
    market = _read(tmp_path, _inputs())
    row = _row(market, initial=Portfolio(9000, {BTC: 0.0, ETH: 10.0}))
    assert all(fill["symbol"] != BTC for fill in row["fills"])
    assert {"symbol": BTC, "side": "buy", "reason": "unverified_current_marks_or_status"} in row["rejected"]


def test_first_trade_cannot_be_replaced_by_hourly_bar_or_later_trade(tmp_path):
    rows = _inputs()
    rows["trades.json"].insert(0, _trade_row(DAY0, BTC, 90, offset=2, label="later"))
    market = _read(tmp_path, rows)
    assert market.first_trade(DAY0, BTC).price == 100
    rows["trades.json"][0]["source"] = "okx:1H:confirmed"
    with pytest.raises(ValueError, match="unaggregated"):
        _read(tmp_path, rows)


def test_same_symbol_first_trade_with_ambiguous_clock_tie_refuses_fill(tmp_path):
    rows = _inputs()
    rows["trades.json"].append(_trade_row(DAY0, BTC, 1, offset=1, label="ambiguous"))
    market = _read(tmp_path, rows)
    assert market.first_trade(DAY0, BTC) is None
    assert [str(DAY0), BTC] in market.coverage()["missing"]["first_trade"]


def test_missing_status_and_causal_prefix_gap_reject_trade_but_future_gap_cannot_cancel_it(tmp_path):
    rows = _inputs()
    states = rows["status.json"]
    states[:] = [r for r in states if not (r["kind"] == "feed" and r["symbol"] == BTC)]
    states.append(_status_row(DAY0, BTC, "feed", start=at(DAY0, 2) + timedelta(microseconds=1)))
    market = _read(tmp_path, rows)
    assert market.first_trade(DAY0, BTC) is None
    assert [str(DAY0), BTC] in market.coverage()["missing"]["trade_feed"]
    assert _row(market)["fills"] == []

    states[-1] = _status_row(DAY0, BTC, "feed", end=at(DAY0, 2) + timedelta(seconds=1.5))
    market = _read(tmp_path, rows)
    assert [fill["symbol"] for fill in _row(market)["fills"]] == [BTC]
    assert [str(DAY0), BTC] in market.coverage()["missing"]["trade_feed"]

    states[:] = [r for r in states if not (r["kind"] in {"check", "state"} and r["symbol"] == BTC)]
    market = _read(tmp_path, rows)
    assert not market.live_at_check(BTC, DAY0)
    assert [str(DAY0), BTC] in market.coverage()["missing"]["execution_state"]
    assert _row(market)["fills"] == []


@pytest.mark.parametrize("state", ["suspend", "delisted", "maintenance", "unknown"])
def test_status_blocks_own_fill_and_persists_exit_until_valid_window(tmp_path, state):
    rows = _inputs(DAY0 + 2 * DAY)
    for row in rows["status.json"]:
        if row["day"] == str(DAY0) and row["symbol"] == BTC and row["kind"] in {"check", "state"}:
            row["state"] = state
    market = _read(tmp_path, rows)
    ledger, _ = simulate(
        market, 20, start=DAY0, end=DAY0 + 2 * DAY, initial=Portfolio(8750, {BTC: 12.5, ETH: 0.0})
    )
    assert not ledger[0]["fills"]
    assert ledger[0]["pending_exits"][BTC]["target"] == 0.0
    assert ledger[1]["exit_completions"] == [{"symbol": BTC, "target": 0.0, "days": 1}]


def test_reversed_venue_clock_does_not_use_peer_future_marks(tmp_path):
    rows = _inputs()
    rows["trades.json"][0] = _trade_row(DAY0, BTC, 120, offset=1, receipt=5)
    rows["trades.json"][1] = _trade_row(DAY0, ETH, 100, offset=2, receipt=2)
    row = _row(_read(tmp_path, rows))
    assert [fill["symbol"] for fill in row["fills"]] == [ETH]
    assert {"symbol": BTC, "reason": "cross_venue_clock_reversal"} in row["rejected"]


def test_passive_half_budgets_and_incomplete_pair_not_certified(tmp_path):
    rows = _inputs()
    market = _read(tmp_path, rows)
    row = _row(market, passive=True)
    assert not row["passive_incomplete"]
    assert [fill["notional_usdt"] + fill["fee_usdt"] for fill in row["fills"]] == pytest.approx([5000, 5000])
    rows["trades.json"].pop()
    assert _row(_read(tmp_path, rows), passive=True)["passive_incomplete"]


def test_source_receipts_and_manifest_hashes_are_required(tmp_path):
    body, manifest = _pack(tmp_path, _inputs(), receipts=False)
    assert inspect_manifest(body, expected_hash=digest(body)) == manifest
    with pytest.raises(ValueError, match="manifest SHA"):
        inspect_manifest(body, expected_hash=AUDIT_HASH)
    with pytest.raises(ValueError, match="source receipt"):
        read_evidence(tmp_path, manifest)
    (tmp_path / "trades.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="evidence checksum"):
        read_evidence(tmp_path, manifest)
    row = _status_row(DAY0, BTC, "check", start=at(DAY0, 1))
    with pytest.raises(ValueError, match="before 01"):
        _status(None, row)


def test_synthetic_pressure_uses_v2_open_events():
    results = run_stress(synthetic_replay)
    assert len(results) == 6
    assert all(all(row["protections"].values()) for row in results.values())
    assert results["combined"]["rejected_sell_attempts"] == 6
    assert results["combined"]["max_exit_delay_days"] == 3
    recovered = results["combined"]["ledger"][3]
    assert all(fill["source"] == TRADE_SOURCE for fill in recovered["fills"])


@pytest.fixture
def authority(tmp_path):
    private = tmp_path / "local-synthetic-signing-key"
    public = tmp_path / "local-synthetic-public-key"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)], check=True)
    state = tmp_path / "state"
    state.mkdir()
    identity = gate.ReviewAuthority(
        "synthetic-reviewer",
        public.read_bytes(),
        digest(public.read_bytes()),
        "a" * 40,
        gate.source_sha256(),
        gate.command_sha256(),
        state,
    )

    def sign(body):
        path = tmp_path / "signed-body"
        path.write_bytes(body)
        return subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(private), "-rawin", "-in", str(path)],
            check=True,
            capture_output=True,
        ).stdout

    return identity, sign


def _audit(accesses=()):
    return gate.report_bytes(
        {
            "initial_reserve_sha256": INITIAL_RESERVE_SHA256,
            "bounds": bounds(),
            "research_accesses": list(accesses),
            "reserve_log_sha256": AUDIT_HASH,
            "run_log_sha256": AUDIT_HASH,
            "prior_exposure_sha256": AUDIT_HASH,
        }
    )


def _cert(identity, manifest_hash, audit, *, kind="isolation", **extra):
    return gate.report_bytes(
        {
            "schema": 1,
            "kind": kind,
            "reviewer_id": identity.reviewer_id,
            "issued_at": _z(at(END, 1)),
            "prereg_sha256": PREREG_SHA256,
            "config_sha256": V1_CONFIG_SHA256,
            "revision": identity.revision,
            "implementation_sha256": identity.implementation_sha256,
            "commands_sha256": identity.commands_sha256,
            "manifest_sha256": manifest_hash,
            "audit_sha256": digest(audit),
            **extra,
        }
    )


def test_default_review_refuses_to_read_anything_and_inspect_only_reads_manifest(tmp_path):
    command = [sys.executable, "-m", "research.spot_defense.v2.run"]
    denied = subprocess.run([*command, "review", *["absent"] * 4], capture_output=True)
    assert denied.returncode == 2
    assert b"authority has not been frozen" in denied.stderr
    body, manifest = _pack(tmp_path, _inputs())
    (tmp_path / "trades.json").unlink()
    called = subprocess.run(
        [*command, "inspect", str(tmp_path / "manifest.json"), digest(body)], check=True, capture_output=True
    )
    assert json.loads(called.stdout) == manifest


def test_gate_refuses_early_untrusted_wrong_scope_and_exposed_paths(authority):
    identity, sign = authority
    audit = _audit()
    iso = _cert(identity, AUDIT_HASH, audit)
    with pytest.raises(ValueError, match="authority"):
        gate.authorize(None, at(END, 1), iso, sign(iso), audit)
    with pytest.raises(ValueError, match="remains sealed"):
        gate.authorize(identity, at(END), iso, sign(iso), audit)
    with pytest.raises(ValueError, match="invalid independent"):
        gate.authorize(identity, at(END, 1), iso, sign(iso + b" "), audit)
    wrong = json.loads(iso)
    wrong["implementation_sha256"] = AUDIT_HASH
    body = gate.report_bytes(wrong)
    with pytest.raises(ValueError, match="scope"):
        gate.authorize(identity, at(END, 1), body, sign(body), audit)
    exposed = _audit(["synthetic prior read"])
    iso = _cert(identity, AUDIT_HASH, exposed)
    with pytest.raises(ValueError, match="exposure reported"):
        gate.authorize(identity, at(END, 1), iso, sign(iso), exposed)


def test_authorized_review_then_single_synthetic_evaluation(tmp_path, authority):
    identity, sign = authority
    snapshot = tmp_path / "synthetic"
    manifest, _ = _pack(snapshot, _inputs(END))
    audit = _audit()
    iso = _cert(identity, digest(manifest), audit)
    only_review = gate.authorize(identity, at(END, 2), iso, sign(iso), audit)
    result = review(snapshot, only_review)
    assert result["coverage"]["complete"]
    assert set(result) == {"manifest_sha256", "isolation_sha256", "coverage"}
    assert not any(identity.state_directory.iterdir())
    with pytest.raises(ValueError, match="release approval"):
        evaluate(snapshot, only_review)
    bad = _cert(
        identity,
        digest(manifest),
        audit,
        kind="release",
        isolation_sha256=digest(iso),
        review_sha256=AUDIT_HASH,
    )
    approval = gate.authorize(
        identity, at(END, 2), iso, sign(iso), audit, release=bad, release_signature=sign(bad)
    )
    with pytest.raises(ValueError, match="differs from signed review"):
        evaluate(snapshot, approval)
    release = _cert(
        identity,
        digest(manifest),
        audit,
        kind="release",
        isolation_sha256=digest(iso),
        review_sha256=digest(gate.report_bytes(result)),
    )
    approval = gate.authorize(
        identity, at(END, 2), iso, sign(iso), audit, release=release, release_signature=sign(release)
    )
    final = evaluate(snapshot, approval)
    assert set(final["by_cost_bp"]) == {20, 40, 60}
    assert final["stress_protections_passed"]
    with pytest.raises(FileExistsError):
        evaluate(snapshot, approval)


def test_signed_coverage_claim_cannot_override_actual_gap(tmp_path, authority):
    identity, sign = authority
    rows = _inputs(END)
    rows["status.json"].pop()
    audited = tmp_path / "gap"
    manifest, _ = _pack(audited, rows)
    audit = _audit()
    iso = _cert(identity, digest(manifest), audit)
    approved = gate.authorize(identity, at(END, 2), iso, sign(iso), audit)
    with pytest.raises(ValueError, match="actual coverage"):
        review(audited, approved)

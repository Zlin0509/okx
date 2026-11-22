import copy
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from research.spot_defense.data import FX
from research.spot_defense.engine import Portfolio
from research.spot_defense.protocol import DAY, SYMBOLS, at, days
from research.spot_defense.run import write_json
from research.spot_defense.stress import run_stress
from research.spot_defense.v2 import gate, launcher
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
from research.spot_defense.v2.run import _assess, evaluate, review

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


def _status_row(day, symbol, kind, state=None, *, start=None, end=None, received=None):
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
        "received_at": _z(start if received is None else received),
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


def _pack(root, inputs, *, counts=None, receipts=True, original=None, conflicts=0, profile="synthetic"):
    files = copy.deepcopy(inputs)
    kinds = {"availability.json": "daily", "trades.json": "trade"}
    receipts_from_rows = {
        (
            row.get("kind", kinds.get(name)),
            row["symbol"],
            row["source"],
            row["received_at"],
            row["response_sha256"],
        )
        for name, rows in files.items()
        for row in rows
    }
    files["provenance.json"] = (
        [
            {
                "kind": kind,
                "symbol": symbol,
                "source": source,
                "received_at": received,
                "response_sha256": sha,
            }
            for kind, symbol, source, received, sha in sorted(receipts_from_rows)
        ]
        if receipts
        else []
    )
    root.mkdir(exist_ok=True)
    custody = (
        original
        if original is not None
        else gate.report_bytes(
            {
                "schema": "zlin21-v2-custody-1",
                "profile": profile,
                "bounds": bounds(),
                "receipts": files["provenance.json"],
            }
        )
    )
    (root / "original-custody.json").write_bytes(custody)
    manifest = {
        "schema": "zlin21-v2-evidence-2",
        "profile": profile,
        "bounds": bounds(),
        "files": {},
        "source_manifest_sha256": digest(custody),
        "missing_counts": counts or dict.fromkeys(KINDS, 0),
        "conflict_count": conflicts,
    }
    assert set(files) == FILES
    for name, rows in files.items():
        body = gate.report_bytes(rows)
        (root / name).write_bytes(body)
        manifest["files"][name] = {"bytes": len(body), "records": len(rows), "sha256": digest(body)}
    body = gate.report_bytes(manifest)
    (root / "manifest.json").write_bytes(body)
    return body, inspect_manifest(body, expected_hash=digest(body))


def _custody(root):
    return (root / "original-custody.json").read_bytes()


def _read(tmp_path, inputs):
    _, manifest = _pack(tmp_path, inputs)
    return read_evidence(tmp_path, manifest, _custody(tmp_path))


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
        read_evidence(tmp_path, manifest, _custody(tmp_path))
    (tmp_path / "trades.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="evidence checksum"):
        read_evidence(tmp_path, manifest, _custody(tmp_path))
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
    state.mkdir(mode=0o700)
    python = Path("/usr/bin/python3").resolve()
    key = public.read_bytes()
    config = {
        "schema": 1,
        "profile": "synthetic",
        "reviewer_id": "synthetic-reviewer",
        "reviewer_public_key": key.decode("ascii"),
        "reviewer_key_sha256": digest(key),
        "revision": "a" * 40,
        "implementation_sha256": gate.source_sha256(),
        "commands_sha256": gate.command_sha256(),
        "launcher_sha256": digest(launcher.SCRIPT.read_bytes()),
        "verifier_path": str(launcher.VERIFIER),
        "verifier_sha256": digest(launcher.VERIFIER.read_bytes()),
        "verifier_version": subprocess.check_output(
            [str(launcher.VERIFIER), "version"], text=True, env=launcher.VERIFIER_ENV
        ).rstrip("\n"),
        "python_path": str(python),
        "python_sha256": digest(python.read_bytes()),
        "state_directory": str(state),
        "state_uid": os.geteuid(),
        "research_uid": None,
        "synthetic_now": _z(at(END, 2)),
    }
    body = gate.report_bytes(config)
    path = tmp_path / "synthetic-authority.json"
    path.write_bytes(body)
    identity = gate.ReviewAuthority(
        config["reviewer_id"],
        key,
        digest(key),
        config["revision"],
        gate.source_sha256(),
        gate.command_sha256(),
        state,
        "synthetic",
        digest(body),
        launcher.VERIFIER,
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
            "profile": identity.profile,
            "authority_sha256": identity.binding_sha256,
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
    denied = subprocess.run([*command, "review", *["absent"] * 5], capture_output=True)
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
    result = review(snapshot, _custody(snapshot), only_review)
    assert result["coverage"]["complete"]
    assert set(result) == {
        "manifest_sha256",
        "authority_sha256",
        "source_manifest_sha256",
        "isolation_sha256",
        "coverage",
    }
    assert not any(identity.state_directory.iterdir())
    with pytest.raises(ValueError, match="release approval"):
        evaluate(snapshot, _custody(snapshot), only_review)
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
        evaluate(snapshot, _custody(snapshot), approval)
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
    final = evaluate(snapshot, _custody(snapshot), approval)
    assert set(final["by_cost_bp"]) == {20, 40, 60}
    assert final["stress_protections_passed"]
    with pytest.raises(FileExistsError):
        evaluate(snapshot, _custody(snapshot), approval)


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
        review(audited, _custody(audited), approved)


def test_check_receipt_timing_and_cannot_backdate(tmp_path):
    timely = _status_row(DAY0, BTC, "check")
    assert _read(tmp_path, _inputs()).live_at_check(BTC, DAY0)
    for late in (at(DAY0, 1), at(DAY0, 1) + timedelta(microseconds=1)):
        with pytest.raises(ValueError, match="before 01"):
            _status(None, timely | {"received_at": _z(late)})
    with pytest.raises(ValueError, match="does not match the original"):
        _status(None, timely | {"start": _z(at(DAY0))})


@pytest.mark.parametrize("kind", ["state", "feed"])
def test_late_continuity_receipt_is_rejected(tmp_path, kind):
    late = _status_row(DAY0, BTC, kind, received=at(DAY0, 2) + timedelta(microseconds=1))
    rows = _inputs()
    rows["status.json"].append(late)
    with pytest.raises(ValueError, match="cannot prove earlier continuity"):
        _read(tmp_path, rows)


def test_original_custody_receipts_bind_kind_time_and_hash(tmp_path):
    rows = _inputs()
    _, manifest = _pack(tmp_path, rows)
    original = _custody(tmp_path)
    with pytest.raises(ValueError, match="original custodian manifest SHA"):
        read_evidence(tmp_path, manifest, original + b" ")
    target = next(row for row in rows["status.json"] if row["kind"] == "feed")
    for field, value in (
        ("received_at", _z(at(DAY0, 2) - timedelta(seconds=1))),
        ("response_sha256", "b" * 64),
    ):
        changed = copy.deepcopy(rows)
        next(row for row in changed["status.json"] if row == target)[field] = value
        _, declared = _pack(tmp_path, changed, original=original)
        with pytest.raises(ValueError, match="differs from original custodian"):
            read_evidence(tmp_path, declared, original)


def _halt_then_reopen(inputs):
    states = inputs["status.json"]
    states[:] = [
        row
        for row in states
        if not (row["day"] == str(DAY0) and row["symbol"] == BTC and row["kind"] == "state")
    ]
    start = at(DAY0, 2) + timedelta(seconds=2)
    end = at(DAY0, 2) + timedelta(seconds=4)
    states.extend(
        [
            _status_row(DAY0, BTC, "state", end=start),
            _status_row(DAY0, BTC, "state", "suspend", start=start, end=end),
            _status_row(DAY0, BTC, "state", start=end),
        ]
    )


def test_later_trade_during_halt_conflicts_even_if_received_after_reopen(tmp_path, authority):
    identity, sign = authority
    rows = _inputs(END)
    _halt_then_reopen(rows)
    conflicting = _trade_row(DAY0, BTC, 100, offset=3, receipt=6)
    rows["trades.json"].append(conflicting)
    body, _ = _pack(tmp_path, rows, conflicts=1)
    audit = _audit()
    iso = _cert(identity, digest(body), audit)
    approved = gate.authorize(identity, at(END, 2), iso, sign(iso), audit)
    result = review(tmp_path, _custody(tmp_path), approved)
    coverage = result["coverage"]
    assert not coverage["complete"]
    assert not any(coverage["missing"].values())
    assert coverage["conflicts"] == [
        {
            "day": str(DAY0),
            "symbol": BTC,
            "trade_id": conflicting["frame"]["data"][0]["tradeId"],
            "exchange_at": at(DAY0, 2).replace(second=3).isoformat(),
            "state": "suspend",
        }
    ]
    release = _cert(
        identity,
        digest(body),
        audit,
        kind="release",
        isolation_sha256=digest(iso),
        review_sha256=digest(gate.report_bytes(result)),
    )
    approved = gate.authorize(
        identity, at(END, 2), iso, sign(iso), audit, release=release, release_signature=sign(release)
    )
    with pytest.raises(ValueError, match="coverage is incomplete"):
        evaluate(tmp_path, _custody(tmp_path), approved)
    assert not any(identity.state_directory.iterdir())


def test_verified_halt_with_no_trade_is_not_missing_coverage(tmp_path):
    rows = _inputs(END)
    for row in rows["status.json"]:
        if row["day"] == str(DAY0) and row["symbol"] == BTC and row["kind"] in ("check", "state"):
            row["state"] = "suspend"
    rows["trades.json"] = [
        row for row in rows["trades.json"] if (row["day"], row["symbol"]) != (str(DAY0), BTC)
    ]
    market = _read(tmp_path, rows)
    assert market.coverage()["complete"]
    row = _row(market, initial=Portfolio(8750, {BTC: 12.5, ETH: 0.0}))
    assert row["signals"][BTC]["known_interruption"]
    assert row["rejected"][0]["reason"] == "verified_nontrading_interval"
    assert row["execution_window_evidence"][BTC]["state_complete"]
    rows["status.json"] = [
        row
        for row in rows["status.json"]
        if not (row["day"] == str(DAY0) and row["symbol"] == BTC and row["kind"] in ("check", "state"))
    ]
    row = _row(_read(tmp_path, rows), initial=Portfolio(8750, {BTC: 12.5, ETH: 0.0}))
    assert not row["signals"][BTC]["known_interruption"]
    assert row["rejected"][0]["reason"] == "market_or_state_evidence_gap"


@pytest.mark.parametrize("gap", ["offline_check", "missing_close", "window"])
def test_passive_requires_fx_evidence(tmp_path, gap):
    rows = _inputs()
    if gap == "missing_close":
        rows["availability.json"] = [
            row for row in rows["availability.json"] if (row["day"], row["symbol"]) != (str(DAY0 - DAY), FX)
        ]
    else:
        for row in rows["status.json"]:
            if row["symbol"] == FX and row["kind"] == ("check" if gap == "offline_check" else "state"):
                row["state"] = "offline"
    assert not _row(_read(tmp_path, rows), passive=True)["fills"]


def test_stale_valuation_suspected_exposure_and_unpriced_zero_leg(tmp_path):
    rows = _inputs()
    good = _read(tmp_path, rows)
    passive = [_row(good, passive=True)]
    rows["trades.json"] = [row for row in rows["trades.json"] if row["symbol"] != BTC]
    market = _read(tmp_path, rows)
    empty_leg = _row(market)
    assert empty_leg["exposure_measured"]
    assert not empty_leg["execution_valuation_stale_assets"]
    hold = _row(market, initial=Portfolio(5000, {BTC: 50, ETH: 0.0}))
    assert not hold["exposure_measured"]
    assert hold["execution_valuation_stale_assets"] == [BTC]
    result = _assess([hold], 10_000, passive, True)
    assert result["acceptance"] == "insufficient_evidence"
    assert result["post_exposure_breaches_stale"] == [str(DAY0)]
    assert result["event_exposure_suspected_stale_days"] == [str(DAY0)]


def test_canonical_v1_stress_artifact_matches_original_delivery(tmp_path):
    output = tmp_path / "stress.json"
    write_json(output, run_stress())
    assert digest(output.read_bytes()) == "5fe4af89a0562b80fc4d4d39795284f023546e59718e353269f7674bfde9fd24"


def _launch(*args):
    command = ["/usr/bin/python3", "-I", "-B", str(launcher.SCRIPT)]
    return subprocess.run([*command, *map(str, args)], capture_output=True, timeout=20)


def _fixture_file(root, name, body):
    path = root / name
    path.write_bytes(body)
    return path


def test_real_cli_approval_binding_and_one_use(tmp_path, authority):
    identity, sign = authority
    config = tmp_path / "synthetic-authority.json"
    root = tmp_path / "fixture"
    manifest, _ = _pack(root, _inputs(END))
    audit = _audit()
    iso = _cert(identity, digest(manifest), audit)
    args = [
        root,
        root / "original-custody.json",
        _fixture_file(tmp_path, "iso.json", iso),
        _fixture_file(tmp_path, "iso.sig", sign(iso)),
        _fixture_file(tmp_path, "audit.json", audit),
    ]
    trusted = ["--synthetic", config, identity.binding_sha256]

    production = _launch("review", *args)
    assert production.returncode == 2
    assert any(
        message in production.stderr
        for message in (
            b"production trust root has not been provisioned",
            b"outside the provisioned installation",
        )
    )
    identity.state_directory.chmod(0o770)
    try:
        wrong_permissions = _launch(*trusted, "review", *args)
        assert wrong_permissions.returncode == 2
        assert b"private to the evaluation account" in wrong_permissions.stderr
    finally:
        identity.state_directory.chmod(0o700)
    unknown = _launch("--synthetic", config, AUDIT_HASH, "review", *args)
    assert unknown.returncode == 2
    assert b"trusted pin" in unknown.stderr
    altered = json.loads(config.read_bytes())
    altered["verifier_version"] = "unbound verifier"
    altered_body = gate.report_bytes(altered)
    altered_path = _fixture_file(tmp_path, "alternate-authority.json", altered_body)
    bad_verifier = _launch("--synthetic", altered_path, digest(altered_body), "review", *args)
    assert bad_verifier.returncode == 2
    assert b"verifier version differs" in bad_verifier.stderr

    alternate = tmp_path / "alternate-state"
    alternate.mkdir(mode=0o700)
    altered = json.loads(config.read_bytes()) | {"state_directory": str(alternate)}
    altered_body = gate.report_bytes(altered)
    altered_path.write_bytes(altered_body)
    wrong_scope = _launch("--synthetic", altered_path, digest(altered_body), "review", *args)
    assert wrong_scope.returncode == 2
    assert b"approval scope" in wrong_scope.stderr

    reviewed = _launch(*trusted, "review", *args)
    assert reviewed.returncode == 0, reviewed.stderr
    report = json.loads(reviewed.stdout)
    assert report["coverage"]["complete"]
    assert report["authority_sha256"] == identity.binding_sha256
    assert not any(identity.state_directory.iterdir())
    release = _cert(
        identity,
        digest(manifest),
        audit,
        kind="release",
        isolation_sha256=digest(iso),
        review_sha256=digest(gate.report_bytes(report)),
    )
    args.extend(
        [
            _fixture_file(tmp_path, "release.json", release),
            _fixture_file(tmp_path, "release.sig", sign(release)),
        ]
    )
    result = _launch(*trusted, "evaluate", *args)
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)["by_cost_bp"]) == {"20", "40", "60"}
    second = _launch(*trusted, "evaluate", *args)
    assert second.returncode == 2 and b"evaluation already claimed" in second.stderr
    assert not any(alternate.iterdir())


def test_synthetic_launcher_refuses_production_profile(tmp_path, authority):
    identity, sign = authority
    root = tmp_path / "fixture"
    body, _ = _pack(root, _inputs(), profile="production")
    audit = _audit()
    iso = _cert(identity, digest(body), audit)
    denied = _launch(
        "--synthetic",
        tmp_path / "synthetic-authority.json",
        identity.binding_sha256,
        "review",
        root,
        root / "original-custody.json",
        _fixture_file(tmp_path, "iso.json", iso),
        _fixture_file(tmp_path, "iso.sig", sign(iso)),
        _fixture_file(tmp_path, "audit.json", audit),
    )
    assert denied.returncode == 2 and b"evidence profile" in denied.stderr

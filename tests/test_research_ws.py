"""Synthetic/mock only. The authorized live archive is an explicit integration, never a fixture."""

import copy
import io
import json
import zipfile
from dataclasses import FrozenInstanceError

import pytest

from research.decision_view import Observed
from research.engine import Rejected
from research.execution_audit import sha
from research.ws_package import read_capture, verifier
from research.ws_replay import normalize, replay

SECOND = 1_000_000_000
SPEC = {
    "symbols": ["A", "B"],
    "channels": ["tickers", "mark-price", "funding-rate"],
    "source": "mock://public",
    "origin": "synthetic_test",
    "grid_ns": SECOND,
    "price_max_age_ns": 2 * SECOND,
    "funding_snapshot_max_age_ns": 120 * SECOND,
}


def record(kind, ns, **extra):
    return {
        "kind": kind,
        "source": SPEC["source"],
        "receive_unix_ns": ns,
        "receive_utc": "SYNTHETIC recorded text",
        **extra,
    }


def data(symbol, channel, ns, ts=200, value="10", state="settled"):
    row = {"instId": symbol, "ts": str(ts)}
    row.update(
        {"markPx": value}
        if channel == "mark-price"
        else {"last": value}
        if channel == "tickers"
        else {
            "settState": state,
            "fundingRate": "-0.001",
            "settFundingRate": "0.002",
            "prevFundingTime": "0",
            "fundingTime": "10000",
            "nextFundingTime": "20000",
        }
    )
    return record(
        "message", ns, raw_text=json.dumps({"arg": {"instId": symbol, "channel": channel}, "data": [row]})
    )


def fixture():
    records = [
        record("connect_attempt", 0),
        record("handshake", 100_000_000, http_status=101),
        record("subscription_sent", 200_000_000),
    ]
    records += [
        data(s, c, 300_000_000 + i, value=str(10 + i))
        for i, (s, c) in enumerate((s, c) for s in SPEC["symbols"] for c in SPEC["channels"])
    ]
    records.append(record("local_close", 4_500_000_000))
    return records


def add(records, *extra):
    return sorted(records + list(extra), key=lambda r: r["receive_unix_ns"])


def run(records, callback=None):
    events, quality = normalize(records, SPEC)
    frames, stream = replay(events, SPEC, on_frame=callback)
    return events, frames, quality, stream


def test_market_bridge_is_readonly_intraday_not_daily_and_never_grants_permission():
    _, frames, _, _ = run(fixture())
    first = frames[0]
    assert isinstance(first.intraday_observation(), Observed)
    assert first.intraday_observation().available_at == 1000
    assert first.daily_observations == ()
    assert first.serialize()["account_equity"] is None
    assert first.serialize()["execution_prices"] is None
    with pytest.raises(Rejected) as error:
        first.require_model()
    assert error.value.code == "historical_qualification_unknown"
    with pytest.raises(TypeError):
        first.components["A/mark-price"]["payload"]["markPx"] = "999"
    with pytest.raises(FrozenInstanceError):
        first.available_ns = 20 * SECOND
    with pytest.raises(AttributeError):
        _ = first.future_events
    assert all(v["available_ns"] <= first.available_ns for v in first.components.values())


def test_future_perturbation_cannot_change_past_public_frames_or_delivered_values():
    records = add(
        fixture(),
        data("A", "mark-price", 2_200_000_000, ts=2200, value="30"),
        data("B", "mark-price", 2_200_000_001, ts=2200, value="40"),
    )
    changed = copy.deepcopy(records)
    for row in changed:
        if row["receive_unix_ns"] > 2 * SECOND and row["kind"] == "message":
            payload = json.loads(row["raw_text"])
            payload["data"][0]["markPx"] = "999999"
            row["raw_text"] = json.dumps(payload)
    seen = []
    _, before, _, _ = run(records, lambda f: seen.append(f.serialize()))
    _, after, _, _ = run(changed)
    assert [f.serialize() for f in before if f.available_ns <= 2 * SECOND] == [
        f.serialize() for f in after if f.available_ns <= 2 * SECOND
    ]
    assert seen[:2] == [f.serialize() for f in after[:2]]
    assert before[2].serialize() != after[2].serialize()
    assert before[0].fresh_marks["A"] != "999999"


def test_fractional_receive_time_ceil_never_releases_at_earlier_millisecond_or_grid():
    rows = fixture()
    for row in rows:
        if row["kind"] == "message" and json.loads(row["raw_text"])["arg"] == {
            "instId": "A",
            "channel": "mark-price",
        }:
            row["receive_unix_ns"] = SECOND + 1
    rows.sort(key=lambda r: r["receive_unix_ns"])
    events, frames, *_ = run(rows)
    late = next(e for e in events if e.get("symbol") == "A" and e.get("channel") == "mark-price")
    assert late["available_at_ms"] == 1001
    assert "A" not in frames[0].fresh_marks and "A" in frames[1].fresh_marks


def test_duplicate_same_timestamp_revision_and_late_exchange_do_not_erase_originals():
    original = next(
        r
        for r in fixture()
        if r["kind"] == "message"
        and json.loads(r["raw_text"])["arg"] == {"instId": "A", "channel": "mark-price"}
    )
    duplicate = {**original, "receive_unix_ns": 400_000_000}
    records = add(
        fixture(),
        duplicate,
        data("A", "mark-price", 500_000_000, value="15"),
        data("A", "mark-price", 600_000_000, ts=100, value="22"),
    )
    events, frames, q, _ = run(records)
    stats = q["streams"]["A/mark-price"]
    assert stats["duplicate_payloads"] == 1 and stats["same_timestamp_revisions"] == 1
    assert stats["exchange_regressions"] == 1
    assert sum(e.get("channel") == "mark-price" and e.get("symbol") == "A" for e in events) == 4
    assert frames[0].fresh_marks["A"] == "15"


def test_stale_and_clock_ahead_marks_are_explicit_missing_quality_not_imputed():
    records = add(fixture(), data("A", "mark-price", 600_000_000, ts=10_000, value="15"))
    _, frames, q, _ = run(records)
    assert q["streams"]["A/mark-price"]["negative_latency_rows"] == 1
    assert "A" in frames[0].missing_or_stale_marks
    assert "B" in frames[2].missing_or_stale_marks
    with pytest.raises(Rejected):
        frames[0].intraday_observation()


def test_disconnect_reconnect_clears_old_quotes_and_tail_disconnect_is_reported():
    records = add(
        fixture(),
        record("disconnect", 1_100_000_000),
        record("connect_attempt", 2_100_000_000),
        record("handshake", 2_200_000_000, http_status=101),
        data("A", "mark-price", 2_300_000_000, ts=2300, value="30"),
        record("disconnect", 4_200_000_000),
    )
    _, frames, _, stream = run(records)
    assert not frames[1].connected
    assert "B" in frames[2].missing_or_stale_marks
    assert frames[2].fresh_marks["A"] == "30"
    assert stream["disconnect_windows"] == [
        {"start_available_ns": 1_100_000_000, "end_available_ns": 2_200_000_000},
        {"start_available_ns": 4_200_000_000, "end_available_ns": None},
    ]
    assert stream["connected_at_end"] is False


def test_receive_clock_rollback_is_flagged_and_conservatively_released_in_line_order():
    rows = fixture()
    rows.insert(-1, data("A", "mark-price", 2_500_000_000, ts=2500, value="20"))
    rows.insert(-1, data("B", "mark-price", 1_500_000_000, ts=1500, value="30"))
    events, frames, q, _ = run(rows)
    rollback = next(e for e in events if e["receive_clock_regression"])
    assert rollback["receive_ns"] == 1_500_000_000 and rollback["available_ns"] == 2_500_000_000
    assert q["receive_clock_regressions"] == 1
    assert frames[1].components["B/mark-price"]["payload"]["markPx"] != "30"


def test_funding_processing_to_settled_including_tail_never_creates_cashflows():
    rows = add(
        fixture(),
        data("A", "funding-rate", 700_000_000, ts=700, state="processing"),
        data("A", "funding-rate", 4_200_000_000, ts=4200, state="settled"),
    )
    _, _, _, stream = run(rows)
    assert len(stream["funding_snapshots"]) == 4
    assert stream["funding_state_transitions"][-1]["to"] == "settled"
    assert stream["inferred_settlement_events"] == [] and stream["funding_cashflow"] is None
    assert all(row["inferred_cashflow"] is None for row in stream["funding_snapshots"])


@pytest.mark.parametrize("change", ["source", "symbol", "price", "receive_type"])
def test_invalid_or_mixed_data_fails_closed(change):
    rows = fixture()
    if change == "source":
        rows[3]["source"] = "mock://different_environment"
    elif change == "receive_type":
        rows[3]["receive_unix_ns"] = "123"
    else:
        p = json.loads(rows[4]["raw_text"])
        if change == "symbol":
            p["arg"]["instId"] = "OTHER"
        else:
            p["data"][0]["markPx"] = "0"
        rows[4]["raw_text"] = json.dumps(p)
    with pytest.raises(Rejected):
        normalize(rows, SPEC)


def synthetic_package(tmp_path):
    peer = verifier()
    root = tmp_path / "package"
    root.mkdir()
    members = {name: b"SYNTHETIC fixture only" for name in peer["REQUIRED_MEMBERS"]}
    for name, body in members.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    manifest = {name: {"bytes": len(body), "sha256": sha(body)} for name, body in members.items()}
    (root / "SHA256SUMS.json").write_text(json.dumps(manifest))
    return peer, root, members


@pytest.mark.parametrize(
    "change,code",
    [
        ("hash", "member_sha256_mismatch"),
        ("size", "member_size_mismatch"),
        ("missing", "member_missing"),
        ("manifest", "manifest_missing"),
    ],
)
def test_peer_member_verification_readonly_and_explicit_failures_on_synthetic_files(tmp_path, change, code):
    peer, root, _ = synthetic_package(tmp_path)
    path = root / "capture/raw.jsonl"
    if change == "hash":
        path.write_bytes(b"s" + path.read_bytes()[1:])
    if change == "size":
        path.write_bytes(path.read_bytes() + b"x")
    if change == "missing":
        path.unlink()
    if change == "manifest":
        (root / "SHA256SUMS.json").unlink()
    before = peer["hash_inputs"](root)
    with pytest.raises(peer["VerificationError"]) as error:
        peer["verify_members"](root)
    assert error.value.code == code
    assert peer["hash_inputs"](root) == before


def test_peer_unlisted_file_policy_is_report_only_and_external_raw_anchor_fails(tmp_path):
    peer, root, _ = synthetic_package(tmp_path)
    (root / "extra.txt").write_text("SYNTHETIC unlisted file")
    before = peer["hash_inputs"](root)
    _, extra = peer["verify_members"](root)
    assert extra == {"extra.txt"}
    report = peer["verify"](root, expected_raw_sha256="0" * 64)
    assert report["status"] == "fail"
    assert report["errors"][0]["code"] == "external_raw_sha256_mismatch"
    assert peer["hash_inputs"](root) == before


def archive(tmp_path, members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    body = stream.getvalue()
    p = tmp_path / "SYNTHETIC.zip"
    p.write_bytes(body)
    return p, {
        "zip_sha256": sha(body),
        "raw_member": "capture/raw.jsonl",
        "raw_sha256": sha(members["capture/raw.jsonl"]),
    }


def test_import_reuses_peer_entry_never_executes_archive_scripts_and_leaves_archive_unchanged(
    tmp_path, monkeypatch
):
    peer, root, members = synthetic_package(tmp_path)
    members["SHA256SUMS.json"] = (root / "SHA256SUMS.json").read_bytes()
    members["evil.py"] = b"raise AssertionError('archive scripts must never run')"
    path, anchors = archive(tmp_path, members)
    calls = []

    def mocked(folder, expected_raw_sha256):
        peer["verify_members"](folder)
        calls.append(expected_raw_sha256)
        return {"status": "pass", "input_hashes_unchanged": True, "unlisted_files": ["evil.py"]}

    monkeypatch.setattr("research.ws_package.verifier", lambda: {"verify": mocked})
    before = path.read_bytes()
    raw, report = read_capture(path, anchors)
    assert raw == members["capture/raw.jsonl"] and calls == [anchors["raw_sha256"]]
    assert report["input_archive_unchanged"] and path.read_bytes() == before
    assert report["readonly_check"]["unlisted_files"] == ["evil.py"]


@pytest.mark.parametrize(
    "anchor,code",
    [("zip_sha256", "external_zip_sha256_mismatch"), ("raw_sha256", "external_raw_sha256_mismatch")],
)
def test_import_wrong_external_anchor_refused_before_verifier(tmp_path, monkeypatch, anchor, code):
    path, anchors = archive(tmp_path, {"capture/raw.jsonl": b"SYNTHETIC"})
    anchors[anchor] = "0" * 64
    monkeypatch.setattr("research.ws_package.verifier", lambda: pytest.fail("must reject before peer call"))
    with pytest.raises(Rejected) as error:
        read_capture(path, anchors)
    assert error.value.code == code


def test_import_rejects_path_traversal_without_reading_outside_inputs(tmp_path):
    path, anchors = archive(tmp_path, {"capture/raw.jsonl": b"SYNTHETIC", "../escape": b"SYNTHETIC"})
    with pytest.raises(Rejected) as error:
        read_capture(path, anchors)
    assert error.value.code == "unsafe_archive_member"

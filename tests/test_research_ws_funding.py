"""Standard-library synthetic tests: no archive, fixtures, statistics or market requests."""

import json
import unittest

from research.ws_replay import normalize, replay

SECOND = 1_000_000_000
SPEC = {
    "source": "mock://funding-lifecycle",
    "origin": "synthetic_test",
    "symbols": ["A"],
    "channels": ["funding-rate"],
    "grid_ns": SECOND,
    "price_max_age_ns": 2 * SECOND,
    "funding_snapshot_max_age_ns": 120 * SECOND,
}


def event(kind, ms, **fields):
    return {"kind": kind, "source": SPEC["source"], "receive_unix_ns": ms * 1_000_000, **fields}


def funding(ms, state):
    return event(
        "message",
        ms,
        raw_text=json.dumps(
            {
                "arg": {"instId": "A", "channel": "funding-rate"},
                "data": [{"ts": str(ms), "settState": state}],
            }
        ),
    )


def run(rows):
    events, _ = normalize(rows, SPEC)
    frames, result = replay(events, SPEC)
    return events, frames, result


def prefix():
    return [event("connect_attempt", 0), event("handshake", 10, http_status=101), funding(100, "processing")]


class FundingLifecycleTests(unittest.TestCase):
    def test_disconnected_messages_are_raw_evidence_only_for_each_close_path(self):
        for kind in ["disconnect", "server_close", "transport_error", "error", "local_close", "ws_control"]:
            with self.subTest(kind=kind):
                close = (
                    event("message", 1100, raw_text=json.dumps({"event": "error"}))
                    if kind == "ws_control"
                    else event(kind, 1100)
                )
                events, frames, result = run(
                    prefix()
                    + [close, funding(1200, "settled"), funding(1300, "processing"), event("end", 2500)]
                )
                self.assertEqual(sum(e["kind"] == "data" for e in events), 3)
                self.assertEqual(len(result["funding_snapshots"]), 1)
                self.assertEqual(result["funding_state_transitions"], [])
                self.assertFalse(frames[1].connected)
                self.assertEqual(frames[1].components["A/funding-rate"]["status"], "missing_or_disconnected")

    def test_reconnect_reestablishes_state_and_then_allows_same_connection_transition(self):
        events, frames, result = run(
            prefix()
            + [
                event("disconnect", 1100),
                funding(1200, "settled"),
                event("connect_attempt", 1500),
                funding(1600, "processing"),
                event("handshake", 2100, http_status=101),
                event("checkpoint", 3100),
                funding(3200, "settled"),
                funding(4200, "processing"),
                event("end", 4500),
            ]
        )
        self.assertEqual(sum(e["kind"] == "data" for e in events), 5)
        self.assertEqual(
            [e["payload"]["settState"] for e in result["funding_snapshots"]],
            ["processing", "settled", "processing"],
        )
        self.assertEqual(
            result["funding_state_transitions"],
            [{"symbol": "A", "available_ns": 4200_000_000, "from": "settled", "to": "processing"}],
        )
        self.assertTrue(frames[2].connected)
        self.assertEqual(frames[2].components["A/funding-rate"]["status"], "missing_or_disconnected")
        self.assertEqual(frames[3].components["A/funding-rate"]["payload"]["settState"], "settled")

    def test_handshake_without_attempt_cannot_bridge_old_connection_state(self):
        for status in [101, 403]:
            with self.subTest(status=status):
                _, frames, result = run(
                    prefix()
                    + [
                        event("handshake", 1100, http_status=status),
                        funding(1200, "settled"),
                        event("end", 2500),
                    ]
                )
                self.assertEqual(result["funding_state_transitions"], [])
                self.assertEqual(len(result["funding_snapshots"]), 2 if status == 101 else 1)
                self.assertEqual(frames[1].connected, status == 101)

    def test_initial_and_prehandshake_messages_never_establish_state(self):
        _, _, result = run(
            [
                funding(0, "processing"),
                event("connect_attempt", 100),
                funding(200, "processing"),
                event("handshake", 300, http_status=101),
                funding(400, "settled"),
                event("end", 1500),
            ]
        )
        self.assertEqual(len(result["funding_snapshots"]), 1)
        self.assertEqual(result["funding_state_transitions"], [])

    def test_normal_connection_tail_transition_is_diagnostic_only(self):
        _, _, result = run(prefix() + [funding(1200, "settled"), event("end", 1500)])
        self.assertEqual(len(result["funding_snapshots"]), 2)
        self.assertEqual(
            result["funding_state_transitions"],
            [{"symbol": "A", "available_ns": 1200_000_000, "from": "processing", "to": "settled"}],
        )
        self.assertIsNone(result["funding_cashflow"])
        self.assertEqual(result["inferred_settlement_events"], [])
        self.assertTrue(all(e["inferred_cashflow"] is None for e in result["funding_snapshots"]))

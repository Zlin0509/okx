"""Freeze point-in-time membership, exclusions and exact integer/test-ID registries before statistics."""

from ..execution_audit import canonical, sha
from ..portfolio import DAY
from .contract import protocol
from .universe import candidate_family, pool_at


def build_registry(market, observations, metadata, turnover, *, universe_verified):
    from ..statistics.core import Candidate, test_ids

    p = protocol()
    slots = candidate_family(metadata, p["synthetic"]["core_anchors"])
    registry = []
    for frame in market.frames:
        if frame.ts % DAY or frame.ts // DAY % 7 != p["decision_calendar"]["epoch_day_mod7"]:
            continue
        visible = {t: row for t, row in observations.items() if t < frame.ts and row.available_at <= frame.ts}
        pool = pool_at(frame.ts, metadata, turnover, visible, universe_verified=universe_verified)
        family = candidate_family(pool["members"], p["synthetic"]["core_anchors"])
        admitted = {c["id"] for c in family}
        tests = [
            cfg["id"] + "/" + c["id"] + "/" + test.split(":", 1)[1]
            for cfg in p["configs"]
            for c in family
            for test in test_ids(Candidate(c["id"], tuple(c["symbols"])))
        ]
        n, legs = len(family), sum(len(c["symbols"]) for c in family)
        entry = {
            "decision_ts": frame.ts,
            "origin": "synthetic_test",
            "status": "available" if pool["status"] == "eligible" else "unavailable",
            "pool": pool,
            "family": family,
            "excluded_slots": [
                {**c, "reason": "outside_point_in_time_pool_or_unknown_metadata"}
                for c in slots
                if c["id"] not in admitted
            ],
            "N": n,
            "L": legs,
            "configuration_members": 6 * n,
            "raw_test_ids": sorted(tests),
            "primary_holm_ids": sorted(t for t in tests if "/level:" not in t),
            "BY_EG_ids": sorted(t for t in tests if t.endswith("/eg")),
            "expected_counts": {"raw": 18 * n + 12 * legs, "primary_holm": 18 * n + 6 * legs, "BY_EG": 6 * n},
            "no_statistics_or_returns_used": True,
        }
        assert len(tests) == entry["expected_counts"]["raw"]
        assert len(entry["primary_holm_ids"]) == entry["expected_counts"]["primary_holm"]
        entry["registry_sha256"] = sha(canonical(entry))
        if entry["status"] != "available":
            entry["N"] = entry["L"] = None
            entry["configuration_members"] = None
            entry["expected_counts"] = dict.fromkeys(("raw", "primary_holm", "BY_EG"), None)
            entry["registry_sha256"] = sha(
                canonical({k: v for k, v in entry.items() if k != "registry_sha256"})
            )
        registry.append(entry)
    return registry

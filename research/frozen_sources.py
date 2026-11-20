"""Verify legacy source hashes after the separately frozen CI integration."""

import json

from .engine import Rejected
from .execution_audit import sha

LIVE_FREEZE = "research/experiments/SOURCE_FREEZE_R3_V2.json"
ORIGINALS = "research/legacy_ci_sources/ORIGINALS.json"


def _require_hash(root, path, expected):
    if not expected or not (root / path).is_file() or sha((root / path).read_bytes()) != expected:
        raise Rejected("freeze_mismatch", f"Frozen source changed: {path}")


def verify_historical_sources(root, expected_hashes):
    """Verify live files, plus exact historical bytes for the enumerated CI migration."""
    live = json.loads((root / LIVE_FREEZE).read_bytes())["sha256"]
    _require_hash(root, ORIGINALS, live.get(ORIGINALS))
    _require_hash(root, "research/frozen_sources.py", live.get("research/frozen_sources.py"))
    originals = json.loads((root / ORIGINALS).read_bytes())
    for path, expected in expected_hashes.items():
        original = originals.get(path)
        if original is None or original["sha256"] != expected:
            _require_hash(root, path, expected)
            continue
        archive = original["archive"]
        if live.get(archive) != expected:
            raise Rejected("freeze_mismatch", f"Historical hash missing from live freeze: {archive}")
        _require_hash(root, archive, expected)
        _require_hash(root, path, live.get(path))

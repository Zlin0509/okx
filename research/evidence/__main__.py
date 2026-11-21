"""Emit a JSON data-evidence audit without invoking any real experimental engine."""

import argparse
import json
from pathlib import Path

from ..experiments.input_guard import install as install_private_guard
from ..synthetic_guard import GUARD_SCOPE, install
from .validator import ROOT, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dossier", required=True, type=Path, help="Directory with dossier.json and its members"
    )
    parser.add_argument("--expected-sha256", required=True, help="Detached SHA-256 of dossier.json")
    args = parser.parse_args()
    guard, private = install(), install_private_guard(ROOT)
    output = audit(args.dossier, args.expected_sha256)
    output["guard"] = {
        "network_and_snapshots": dict(guard),
        "controlled_inputs": dict(private),
        "scope": GUARD_SCOPE,
    }
    if any(guard.values()) or any(private.values()):
        output["status"] = "evidence_incomplete"
        output["findings"].append(
            {"artifact": "dossier", "code": "guard_refused_access", "count": 1, "examples": []}
        )
    print(json.dumps(output, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 2 if output["status"] == "evidence_incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())

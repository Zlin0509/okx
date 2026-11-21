"""Create a Chinese offline report, SVG charts and a checksum manifest."""

import argparse
import json
from pathlib import Path

from ..engine import Rejected, serializable
from ..execution_audit import canonical, sha
from ..experiments.input_guard import install as install_private_guard
from ..synthetic_guard import GUARD_SCOPE, install
from .charts import render_charts
from .render import markdown
from .report import ROOT, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r2-reviewed", type=Path, help="Directory of reviewed R2 configuration JSON files")
    parser.add_argument("--r3-reviewed", type=Path, help="Reviewed R3 acceptance.json")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    guard, private = install(), install_private_guard(ROOT)
    if args.output.exists() or args.output.is_symlink():
        parser.exit(2, "output_directory_already_exists\n")
    try:
        result = serializable(report(args.r2_reviewed, args.r3_reviewed))
        result["execution_scope"] = {
            "guard": dict(guard),
            "private_input_guard": dict(private),
            "scope": GUARD_SCOPE,
        }
        if any(guard.values()) or any(private.values()):
            raise Rejected("unexpected_private_input_or_network_access", "Reporting must remain offline")
        files = {
            "results.json": canonical(result),
            "REPORT.zh.md": markdown(result),
            **render_charts(result),
        }
        manifest = {name: {"sha256": sha(body), "bytes": len(body)} for name, body in sorted(files.items())}
        args.output.mkdir(parents=True, exist_ok=False)
        for name, body in files.items():
            (args.output / name).write_bytes(body)
        (args.output / "MANIFEST.json").write_bytes(canonical(manifest))
    except (OSError, Rejected) as exc:
        parser.exit(2, getattr(exc, "code", type(exc).__name__) + "\n")
    ready = sum(
        row["status"] == "available" for config in result["R2"]["runs"].values() for row in config.values()
    )
    summary = {
        "status": "synthetic_report_generated",
        "R2_available_cases": ready,
        "R3_available_scenarios": sum(r["status"] == "available" for r in result["R3"].values()),
        "real_metrics": result["real_metrics"],
        "results_sha256": manifest["results.json"]["sha256"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

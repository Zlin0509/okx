"""M3B synthetic statistics reproduction; imports and execution stay separate from trading."""

import json
import os
import sys

from ..synthetic_guard import GUARD_SCOPE, install

# This changes only the standalone CLI process, before importing numerical libraries.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_name] = "1"

if len(sys.argv) > 1 and sys.argv[1] == "real":
    print("real_statistics_disabled: M3B only accepts synthetic statistical validation", file=sys.stderr)
    raise SystemExit(2)

_guard = install()

try:
    from .runner import main  # noqa: E402
except PermissionError as exc:
    print(
        json.dumps(
            {"synthetic_guard": dict(_guard), "guard_scope": GUARD_SCOPE, "guard_error": str(exc)},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    raise SystemExit(3) from None

main(_guard)

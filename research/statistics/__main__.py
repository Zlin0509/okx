"""M3B synthetic statistics reproduction; imports and execution stay separate from trading."""

import os
import sys

from ..synthetic_guard import install

# This changes only the standalone CLI process, before importing numerical libraries.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_name] = "1"

if len(sys.argv) > 1 and sys.argv[1] == "real":
    print("real_statistics_disabled: M3B only accepts synthetic statistical validation", file=sys.stderr)
    raise SystemExit(2)

_guard = install()

from .runner import main  # noqa: E402

main(_guard)

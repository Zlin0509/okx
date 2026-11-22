"""Frozen UTC calendar and identifiers for the offline v2 candidate."""

from datetime import date, timedelta

from ..protocol import LOCK_END, LOCK_START, at

WARMUP_START = date(2026, 12, 3)
EVAL_START = date(2027, 2, 1)
END = date(2028, 2, 1)
PREREG_SHA256 = "52dc873fb8d1fb11aa2ce16404462188e74577269a375ab496aa4b8de7fc03e7"
V1_CONFIG_SHA256 = "2d09763ecaf7a9665c86881830120dd3688c413ac8ee65ee6ce9059758bcc127"
INITIAL_RESERVE_SHA256 = "81afaa4c20fade0b2c0d7fa3e5386895fdf25392a7e9242197e736eca9e791a3"


def observation_day(day, *, execution=False):
    if LOCK_START <= day <= LOCK_END:
        raise ValueError("permanently sealed v1 market date")
    lower = EVAL_START if execution else WARMUP_START
    if not lower <= day < END:
        raise ValueError("market date outside frozen v2 bounds")
    return day


def bounds():
    return {
        "permanent_v1": [at(LOCK_START).isoformat(), at(LOCK_END + timedelta(days=1)).isoformat()],
        "protected": [at(WARMUP_START).isoformat(), at(END).isoformat()],
        "evaluation": [at(EVAL_START).isoformat(), at(END).isoformat()],
    }

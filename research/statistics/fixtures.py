"""Prespecified seeded synthetic processes. Nothing here is market history."""

import math
import random
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType

from ..decision_view import DecisionSnapshot, Observed
from .core import DAY, Candidate, Eligibility, FamilyPlan, config


def synthetic_series():
    rng = random.Random(config()["seed"])
    series = {}
    x, w, independent, rw_resid = 4.0, 3.0, 4.5, 0.0
    e, eb = 0.0, 0.0
    pivot = None
    for day in range(700):
        x += rng.gauss(0.0005, 0.02)
        w += rng.gauss(0.0003, 0.02)
        independent += rng.gauss(0.0005, 0.02)
        rw_resid += rng.gauss(0, 0.02)
        e = 0.8 * e + rng.gauss(0, 0.002)
        eb = 0.8 * eb + rng.gauss(0, 0.002)
        stationary_x = 4 + rng.gauss(0, 0.01)
        stationary_y = 4.5 + rng.gauss(0, 0.01)
        if day == 256:
            pivot = x
        broken = 0.4 + 1.2 * x + e + (x - pivot if pivot is not None else 0)
        logs = {
            "X": x,
            "W": w,
            "Y": 0.4 + 1.2 * x + e,
            "BASKET": 1 + 0.8 * x + 0.3 * w + eb,
            "INDEPENDENT": independent,
            "RW": 0.4 + 1.2 * x + rw_resid,
            "BROKEN": broken,
            "SX": stationary_x,
            "SY": stationary_y,
        }
        ts = day * DAY + DAY - 1
        prices = MappingProxyType({s: Decimal(format(math.exp(value), ".17g")) for s, value in logs.items()})
        series[ts] = Observed(ts, ts, prices)
    return series


def asof(series, day=512):
    """Fixture driver constructs a detached available snapshot; the statistical layer gets only this."""
    ts = day * DAY + DAY - 1
    observations = {t: row for t, row in series.items() if t <= ts and row.available_at <= ts}
    return DecisionSnapshot(
        ts,
        MappingProxyType(dict(observations[ts].prices)),
        MappingProxyType({}),
        MappingProxyType(observations),
        MappingProxyType({}),
        (),
        (),
        Decimal(10000),
    )


def family():
    candidates = (
        Candidate("pair", ("Y", "X")),
        Candidate("basket", ("BASKET", "X", "W")),
        Candidate("independent", ("INDEPENDENT", "X")),
        Candidate("rw_residual", ("RW", "X")),
        Candidate("break", ("BROKEN", "X")),
        Candidate("stationary_levels", ("SY", "SX")),
    )
    plan = FamilyPlan("prespecified_synthetic_family_1", candidates)
    eligibility = {c.id: Eligibility("verified", 0, "synthetic_metadata_fixture") for c in candidates}
    return plan, eligibility


def changed_future(series, cutoff):
    return {
        t: replace(row, prices=MappingProxyType({s: p * Decimal("1.2") for s, p in row.prices.items()}))
        if t > cutoff
        else row
        for t, row in series.items()
    }

"""Synthetic strategy callback: immutable historical snapshot, detached pool, frozen screen only."""

import math
from dataclasses import asdict, dataclass

from ..decision_view import Decision
from ..engine import ZERO, Leg, Rejected, Signal, number
from ..execution_audit import canonical, sha
from ..portfolio import DAY, allocate, hedge_error
from ..spreads import fit_model, logs, solve
from .contract import protocol
from .screen import check_batch


@dataclass(frozen=True)
class Allowance:
    symbols: tuple
    known_at: int

    def require(self, symbols, ts):
        if symbols != self.symbols or ts < self.known_at:
            raise Rejected("qualification_unknown", "Historical screen must precede signal")


@dataclass(frozen=True)
class TaggedModel:
    price: object
    mode: str
    beta: tuple
    intercept: float
    sd: float
    origin: str
    source: str
    family_sha256: str
    training_sha256: str

    @property
    def symbols(self):
        return self.price.symbols

    @property
    def vector(self):
        return (1.0,) + tuple(-b for b in self.beta)

    def zscore(self, current, previous):
        if self.origin != "synthetic_test" or self.source != "synthetic_statistical_screen_only":
            raise Rejected("real_model_use_disabled", "Synthetic screen-only model")
        if self.mode == "log_price":
            return self.price.zscore(current)
        now, before = logs(current, self.symbols), logs(previous, self.symbols)
        returns = [a - b for a, b in zip(now, before, strict=True)]
        return (
            returns[0]
            - self.intercept
            - math.fsum(b * r for b, r in zip(self.beta, returns[1:], strict=True))
        ) / self.sd


def model_at(frame, candidate, cfg, batch):
    symbols = tuple(candidate["symbols"])
    allowance = Allowance(symbols, batch["decision_ts"])
    rows = [frame.observations[t] for t in sorted(frame.observations) if t < frame.ts]
    model = fit_model(rows, symbols, frame.ts, allowance, cfg["window"])
    used = rows[-cfg["window"] :]
    digest = sha(canonical([[r.ts, r.available_at, {s: str(r.prices[s]) for s in symbols}] for r in used]))
    beta, intercept, sd = model.beta, model.intercept, model.sd
    if cfg["mode"] == "relative_return":
        values = [logs(r.prices, symbols) for r in used]
        returns = [
            [b - a for a, b in zip(before, after, strict=True)]
            for before, after in zip(values, values[1:], strict=False)
        ]
        means = [math.fsum(row[i] for row in returns) / len(returns) for i in range(len(symbols))]
        centered = [[x - mean for x, mean in zip(row, means, strict=True)] for row in returns]
        k = len(symbols) - 1
        beta = solve(
            [[math.fsum(row[i + 1] * row[j + 1] for row in centered) for j in range(k)] for i in range(k)],
            [math.fsum(row[i + 1] * row[0] for row in centered) for i in range(k)],
        )
        intercept = means[0] - math.fsum(b * m for b, m in zip(beta, means[1:], strict=True))
        residuals = [
            row[0] - math.fsum(b * r for b, r in zip(beta, row[1:], strict=True)) for row in centered
        ]
        sd = math.sqrt(math.fsum(e * e for e in residuals) / (len(residuals) - 1))
        if sd < 1e-10:
            raise Rejected("zero_residual_scale", "No return signal from zero scale")
    return TaggedModel(
        model,
        cfg["mode"],
        beta,
        intercept,
        sd,
        "synthetic_test",
        "synthetic_statistical_screen_only",
        batch["family_sha256"],
        digest,
    )


class Strategy:
    def __init__(self, cfg, batch, end_ts):
        check_batch(batch)
        self.cfg, self.batch, self.end_ts = cfg, batch, end_ts
        self.active, self.invalidated, self.events, self.counter = {}, set(), [], 0
        r = protocol()["risk"]
        self.params = {
            "allocation_gross_limit": r["target_gross"],
            "allocation_asset_limit": r["target_asset_absolute"],
        }

    def signal(self, ts, book, targets):
        self.counter += 1
        return Signal(f"experiment-{self.counter}", ts, {Leg(book, s): q for s, q in targets.items()})

    def event(self, ts, book, action, reason, **values):
        self.events.append(
            {"ts": ts, "config": self.cfg["id"], "book": book, "action": action, "reason": reason, **values}
        )

    def __call__(self, frame, pool):
        if frame.ts % DAY != DAY - 1:
            return Decision()
        ts, cfg = frame.ts, self.cfg
        current, previous = frame.observations.get(ts), frame.observations.get(ts - DAY)
        orders, cancels, exited = [], set(), set()
        for book, state in list(self.active.items()):
            held = {
                leg.symbol: p.quantity * frame.marks[leg.symbol]
                for leg, p in frame.positions.items()
                if leg.book == book and p.quantity
            }
            busy = any(p.leg.book == book for p in frame.pending)
            if state["exiting"]:
                if not held and not busy:
                    del self.active[book]
                continue
            reason, z = None, None
            fills = [
                f.ts
                for f in frame.fills
                if f.leg.book == book and f.ts > state["known_at"] and f.executed_delta
            ]
            try:
                if current is None or previous is None:
                    raise Rejected("missing_observation", "Daily signal data unavailable")
                z = state["model"].zscore(current.prices, previous.prices)
            except Rejected:
                reason = "missing_observation"
            if ts >= self.end_ts:
                reason = "boundary_purge"
            elif pool["status"] != "eligible" or not set(state["model"].symbols) <= set(pool["members"]):
                reason = "point_in_time_eligibility_lost"
            elif reason is None:
                if abs(z) >= cfg["break_z"]:
                    reason = "relationship_failure"
                    self.invalidated.add(book)
                elif hedge_error(state["model"], held) > protocol()["risk"]["hedge_error_limit"]:
                    reason = "hedge_failure"
                elif abs(z) <= cfg["exit_z"] or z * state["entry_z"] <= 0:
                    reason = "mean_reversion"
                elif fills and ts - min(fills) >= cfg["timeout_days"] * DAY:
                    reason = "timeout"
            self.event(ts, book, "daily_check", reason or "hold", z=z)
            if reason:
                cancels.add(book)
                orders.append(self.signal(ts, book, dict.fromkeys(state["model"].symbols, ZERO)))
                state["exiting"] = True
                exited.add(book)
                self.event(ts, book, "exit", reason)
        if ts >= self.end_ts or ts // DAY % cfg["decision_every_days"]:
            return Decision(tuple(orders), frozenset(cancels))
        if current is None or previous is None or pool["status"] != "eligible":
            self.event(ts, "*", "rejected", "unknown_pool_or_current_observation")
            return Decision(tuple(orders), frozenset(cancels))
        proposals, models = {}, {}
        reserved = {
            Leg(leg.book, leg.symbol): abs(p.quantity * frame.marks[leg.symbol])
            for leg, p in frame.positions.items()
            if p.quantity
        }
        for book, state in self.active.items():
            for s, quantity in state["targets"].items():
                leg = Leg(book, s)
                reserved[leg] = max(reserved.get(leg, ZERO), abs(quantity * frame.marks[s]))
        for candidate in self.batch["family"]:
            book = candidate["id"]
            if book in self.active or book in exited or book in self.invalidated:
                continue
            permit = self.batch["permits"][cfg["id"] + "/" + book]
            if permit["status"] != "qualified" or not set(candidate["symbols"]) <= set(pool["members"]):
                self.event(ts, book, "rejected", "unqualified_or_outside_point_in_time_pool")
                continue
            try:
                model = model_at(frame, candidate, cfg, self.batch)
                z = model.zscore(current.prices, previous.prices)
                if not cfg["entry_z"] <= abs(z) < cfg["break_z"]:
                    self.event(ts, book, "no_entry", "deviation_outside_entry", z=z)
                    continue
                vector = model.vector
                size = frame.equity * number(protocol()["risk"]["book_gross_fraction"])
                direction = -1 if z > 0 else 1
                norm = number(math.fsum(abs(v) for v in vector))
                proposals[book] = {
                    s: number(v) * direction * size / norm for s, v in zip(model.symbols, vector, strict=True)
                }
                models[book] = (model, z)
            except Rejected as exc:
                self.event(ts, book, "rejected", exc.code)
        allocated, scale = allocate(proposals, reserved, frame.equity, self.params)
        for book, notionals in allocated.items():
            if not scale:
                self.event(ts, book, "rejected", "allocation_capacity")
                continue
            model, z = models[book]
            targets = {s: n / number(current.prices[s]) for s, n in notionals.items()}
            orders.append(self.signal(ts, book, targets))
            self.active[book] = {
                "model": model,
                "entry_z": z,
                "known_at": ts,
                "targets": targets,
                "exiting": False,
            }
            self.event(
                ts,
                book,
                "entry",
                "synthetic_statistical_screen_only",
                model=asdict(model),
                z=z,
                allocation_scale=scale,
            )
        return Decision(tuple(orders), frozenset(cancels))

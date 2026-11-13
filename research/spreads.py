"""Historical-window log-price residual interfaces. No statistical qualification is inferred."""

import math
from dataclasses import dataclass

from .engine import Rejected


@dataclass(frozen=True)
class Observation:
    ts: int
    available_at: int
    prices: dict


@dataclass(frozen=True)
class Qualification:
    symbols: tuple[str, ...]
    available_at: int
    status: str = "unknown"
    source: str = "unknown"

    def require(self, symbols, ts):
        if (
            self.symbols != symbols
            or self.available_at > ts
            or self.status != "qualified"
            or self.source != "synthetic_fixture_truth"
        ):
            raise Rejected(
                "qualification_unknown", "M3 requires explicit, available synthetic truth permission"
            )


def holm(pvalues, family, alpha=0.05):
    """External p-values only; this correction never creates a Qualification."""
    if not family or len(set(family)) != len(family) or set(pvalues) != set(family):
        raise Rejected("incomplete_test_family", "All prespecified tests, including failures, are required")
    if not 0 < alpha < 1 or any(not math.isfinite(p) or not 0 <= p <= 1 for p in pvalues.values()):
        raise Rejected("invalid_pvalue", "Finite p-values in [0,1] required")
    result = {}
    running = 0.0
    for index, (name, pvalue) in enumerate(sorted(pvalues.items(), key=lambda item: (item[1], item[0]))):
        running = max(running, min(1.0, (len(family) - index) * pvalue))
        result[name] = {"adjusted_p": running, "rejected_null": running <= alpha}
    return result


def logs(prices, symbols):
    values = []
    for symbol in symbols:
        try:
            price = float(prices[symbol])
        except (KeyError, ValueError, TypeError) as exc:
            raise Rejected("missing_observation", f"Missing price for {symbol}") from exc
        if not math.isfinite(price) or price <= 0:
            raise Rejected("invalid_price", "Positive finite observed prices required")
        values.append(math.log(price))
    return values


def solve(matrix, vector):
    n = len(vector)
    rows = [list(row) + [value] for row, value in zip(matrix, vector, strict=True)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda i: abs(rows[i][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise Rejected("singular_training", "Constant or collinear hedge series")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        divisor = rows[col][col]
        rows[col] = [x / divisor for x in rows[col]]
        for i in range(n):
            if i != col:
                factor = rows[i][col]
                rows[i] = [x - factor * y for x, y in zip(rows[i], rows[col], strict=True)]
    return tuple(row[-1] for row in rows)


@dataclass(frozen=True)
class Model:
    symbols: tuple[str, ...]
    intercept: float
    beta: tuple[float, ...]
    mean: float
    sd: float
    train_start: int
    train_end: int
    available_at: int
    ar_phi: float | None
    half_life_days: float | None

    def zscore(self, prices):
        y, *xs = logs(prices, self.symbols)
        residual = y - self.intercept - math.fsum(b * x for b, x in zip(self.beta, xs, strict=True))
        return (residual - self.mean) / self.sd

    @property
    def vector(self):
        return (1.0,) + tuple(-b for b in self.beta)


def fit_model(history, symbols, decision_ts, qualification, window=56):
    qualification.require(symbols, decision_ts)
    if len(symbols) < 2 or len(set(symbols)) != len(symbols):
        raise Rejected("invalid_relation", "A dependent asset and distinct hedge assets are required")
    if window < len(symbols) + 2:
        raise Rejected("short_training", "Insufficient degrees of freedom")
    if any(a.ts >= b.ts for a, b in zip(history, history[1:], strict=False)):
        raise Rejected("invalid_history", "History must be unique and chronological")
    prior = [row for row in history if row.ts < decision_ts]
    rows = prior[-window:]
    if len(rows) != window:
        raise Rejected("short_training", "Full lagged training window required")
    if any(row.available_at > decision_ts or row.available_at < row.ts for row in rows):
        raise Rejected("unavailable_training", "Training observation is not available at the decision")
    if any(b.ts - a.ts != 86400000 for a, b in zip(rows, rows[1:], strict=False)):
        raise Rejected("missing_observation", "Training requires consecutive complete UTC days")
    if rows[-1].ts != decision_ts - 86400000:
        raise Rejected("missing_observation", "Latest lagged day must be present")
    # Centre regressors to avoid the poor conditioning of raw log-price normal equations.
    data = [logs(row.prices, symbols) for row in rows]
    means = [math.fsum(row[i] for row in data) / window for i in range(len(symbols))]
    centered = [[value - mean for value, mean in zip(row, means, strict=True)] for row in data]
    k = len(symbols) - 1
    matrix = [[math.fsum(row[i + 1] * row[j + 1] for row in centered) for j in range(k)] for i in range(k)]
    rhs = [math.fsum(row[i + 1] * row[0] for row in centered) for i in range(k)]
    beta = solve(matrix, rhs)
    intercept = means[0] - math.fsum(b * mean for b, mean in zip(beta, means[1:], strict=True))
    residuals = [row[0] - math.fsum(b * x for b, x in zip(beta, row[1:], strict=True)) for row in centered]
    mean = math.fsum(residuals) / window
    sd = math.sqrt(math.fsum((r - mean) ** 2 for r in residuals) / (window - 1))
    if sd < 1e-10:
        raise Rejected("zero_volatility", "Residual scale is zero or numerically insignificant")
    lag = residuals[:-1]
    lead = residuals[1:]
    lag_mean, lead_mean = math.fsum(lag) / len(lag), math.fsum(lead) / len(lead)
    denom = math.fsum((r - lag_mean) ** 2 for r in lag)
    phi = (
        math.fsum((a - lag_mean) * (b - lead_mean) for a, b in zip(lag, lead, strict=True)) / denom
        if denom
        else None
    )
    half = -math.log(2) / math.log(phi) if phi is not None and 0 < phi < 1 else None
    return Model(symbols, intercept, beta, mean, sd, rows[0].ts, rows[-1].ts, decision_ts, phi, half)

from decimal import Decimal


def opening_budget(settings, equity, available, allocation, existing=Decimal(0)):
    """Gross notional budget shared by execution and backtest/selection paths."""
    cap = min(settings.max_position, equity * settings.equity_fraction)
    return max(
        Decimal(0), min(settings.max_order, equity * allocation, cap - existing, available / Decimal("1.01"))
    )

"""Fabricated spot quotes, perpetual fills and validation rows for acceptance diagnostics."""

from dataclasses import asdict, is_dataclass
from pathlib import Path

from ..engine import Costs, Engine, Frame, Leg, Market, Rejected, Signal, number, serializable
from ..execution_audit import canonical, sha
from ..portfolio import DAY
from .acceptance import ScenarioEvidence, joint_acceptance, paired_bootstrap
from .benchmarks import SpotPrice, Tradability, Turnover, spot_benchmarks
from .contract import protocol
from .episodes import EntryIntent, qualifying_round_trips
from .evaluation_inputs import ORIGIN, DailyReturns


def mock_spot(days):
    symbols = ("BTC", "A", "B", "C", "D", "E", "F", "G", "H", "I")
    first = days[0] - 600 * DAY
    listing = Tradability(first, first, first, first, True, True)
    spots = {f"{s}-USDT": (listing,) for s in symbols}
    perps = {f"{s}-SWAP": (listing,) for s in spots}
    volumes = {
        s: {t - 1: Turnover(t - 1, True, 20_000_000) for t in range(days[0] - 30 * DAY, days[-1] + DAY, DAY)}
        for s in perps
    }
    opens, closes = {}, {}
    for i, symbol in enumerate(spots):
        for j, t in enumerate(days):
            v = 100 + i + j * 0.025 + ((j + i) % 9 - 4) * 0.11
            opens[t, symbol] = SpotPrice(v, t)
            close = t + DAY - 1
            closes[close, symbol] = SpotPrice(v + 0.025, close)
    return {
        "origin": ORIGIN,
        "days": days,
        "universe_verified": True,
        "perps": perps,
        "spot": spots,
        "turnover": volumes,
        "opens": opens,
        "closes": closes,
        "btc_entry_known_at": days[0] - DAY,
    }


def mock_ledger(days, scenario, signals):
    costs = Costs(**{k: number(v) for k, v in protocol()["cost_scenarios"][scenario].items()})
    opens = tuple(days)
    frames = []
    for i, t in enumerate(days):
        opening = {"A": 100 + (i % 10) * 0.3, "B": 100}
        frames.extend(
            (
                Frame(t, opening, opening),
                Frame(t + DAY - 1, {"A": opening["A"] + 0.1, "B": 100}, {}),
            )
        )
    calendars = dict.fromkeys(("A", "B"), opens)
    funding = {
        (t, s): number("0.0001" if i % 2 else "-0.0001") for i, t in enumerate(opens) for s in calendars
    }
    market = Market(
        tuple(frames),
        tuple(f.ts for f in frames),
        calendars,
        calendars,
        funding,
        ORIGIN,
        "fabricated daily funding with explicit marks",
    )
    report = Engine(market, costs=costs).run(signals)
    return report["ledger"], {f.ts: f.marks for f in frames}


def mock_validation():
    return {
        c["id"]: {
            "origin": ORIGIN,
            "stage": "validation",
            "status": "available",
            "net_cagr": 0.02,
            "net_sharpe": 1.0 if c["id"] == "primary" else 0.8,
            "turnover": 0.1,
            "data_risk_pass": True,
        }
        for c in protocol()["configs"]
    }


def _daily(ledger, days, scenario):
    closes = {r["ts"]: r["equity"] for r in ledger if r["event"] == "frame_end"}
    capital = protocol()["risk"]["initial_equity"]
    previous, values = number(capital), []
    for day in days:
        current = closes[day + DAY - 1]
        values.append(float(current / previous - 1))
        previous = current
    return DailyReturns(ORIGIN, scenario, capital, days, tuple(values))


def exercise():
    # Calendar starts on a Monday. The week's first eligible basket fills on Tuesday.
    days = tuple((4 + i) * DAY for i in range(365))
    spot_args = mock_spot(days)
    signals, intents = [], {}
    for index in range(32):
        opened = index * 10
        book, entry = f"mock-{index:02d}", f"open-{index:02d}"
        targets = {Leg(book, "A"): number(1), Leg(book, "B"): number(-1)}
        intents[entry] = EntryIntent(book, days[opened], {"A": 1, "B": -1})
        signals.extend(
            (
                Signal(entry, days[opened], targets),
                Signal(f"close-{index:02d}", days[opened + 3], dict.fromkeys(targets, number(0))),
            )
        )
    paths, proofs, results = {}, {}, {}
    for scenario in protocol()["cost_scenarios"]:
        benchmarks = spot_benchmarks(scenario=scenario, **spot_args)
        ledger, marks = mock_ledger(days, scenario, signals)
        trips = qualifying_round_trips(
            origin=ORIGIN, ledger=ledger, intents=intents, marks=marks, days=days, scenario=scenario
        )
        if benchmarks["status"] != "synthetic_diagnostic" or trips["status"] != "synthetic_diagnostic":
            raise Rejected("fixture_setup_failed", str((benchmarks.get("reason"), trips.get("reason"))))
        fills = [r for r in ledger if r["event"] in {"fill", "risk_fill"}]
        capital = protocol()["risk"]["initial_equity"]
        turnover = float(sum(abs(r["executed_delta"]) * r["execution_price"] for r in fills) / capital)
        # This fabricated ledger exercises a deliberately supplied, non-market participation limit.
        daily_notional = {}
        for row in fills:
            key = (row["ts"] // DAY, row["symbol"])
            daily_notional[key] = (
                daily_notional.get(key, 0) + abs(row["executed_delta"]) * row["execution_price"]
            )
        proofs[scenario] = ScenarioEvidence(
            ORIGIN,
            scenario,
            True,
            not any(r["event"] == "risk_halt" for r in ledger),
            max(daily_notional.values()) <= 20_000_000 * protocol()["risk"]["daily_participation"],
            turnover,
        )
        paths[scenario] = {
            "strategy": _daily(ledger, days, scenario),
            **{k: benchmarks[k]["returns"] for k in ("btc", "equal_weight")},
        }
        results[scenario] = {
            "benchmarks": benchmarks,
            "episodes": trips,
            "strategy_daily_returns": paths[scenario]["strategy"],
            "scenario_evidence": asdict(proofs[scenario]),
            "bootstrap": paired_bootstrap(origin=ORIGIN, eligible_days=days, **paths[scenario]),
        }
    return {
        "origin": ORIGIN,
        "performance_evidence": False,
        "real_returns": None,
        "scenarios": results,
        "joint_acceptance": joint_acceptance(
            origin=ORIGIN,
            eligible_days=days,
            base=paths["base"],
            stress=paths["stress"],
            trips=results["base"]["episodes"],
            validation=mock_validation(),
            selected_config="primary",
            proofs=proofs,
        ),
    }


def write_exercise(output):
    output = Path(output)
    if output.exists():
        raise Rejected("frozen_directory_exists", "Use a fresh output directory")
    report = exercise()
    if report["joint_acceptance"]["status"] != "synthetic_diagnostic":
        raise Rejected("incomplete_synthetic_acceptance_diagnostic", str(report["joint_acceptance"]))

    # The output uses dataclass values internally to preserve provenance on detached paths.
    def convert(value):
        if is_dataclass(value):
            return convert(asdict(value))
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [convert(v) for v in value]
        return value

    content = canonical(serializable(convert(report)))
    output.mkdir(parents=True)
    (output / "acceptance.json").write_bytes(content)
    return {
        "status": "synthetic_acceptance_diagnostic_completed",
        "origin": ORIGIN,
        "real_returns": None,
        "acceptance_passed": False,
        "qualifying_synthetic_relationship_trades": report["joint_acceptance"]["qualifying_count"],
        "report_sha256": sha(content),
    }

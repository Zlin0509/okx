"""Synthetic acceptance checks with direct assertions on accounting and boundary semantics."""

from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from research.engine import Leg, Signal, number
from research.experiments.acceptance import ScenarioEvidence, joint_acceptance, paired_bootstrap
from research.experiments.acceptance_fixture import mock_ledger, mock_spot, mock_validation
from research.experiments.benchmarks import SpotPrice, Turnover, spot_benchmarks
from research.experiments.episodes import EntryIntent, qualifying_round_trips
from research.experiments.evaluation_inputs import ORIGIN, DailyReturns
from research.portfolio import DAY


def days(n):
    return tuple((4 + i) * DAY for i in range(n))


def path(values, scenario="base", calendar=None):
    return DailyReturns(ORIGIN, scenario, 10_000, calendar or days(len(values)), tuple(values))


class SpotTests(TestCase):
    def setUp(self):
        self.days = days(10)
        self.args = mock_spot(self.days)

    def run_spot(self, scenario="base", **updates):
        return spot_benchmarks(scenario=scenario, **(self.args | updates))

    def test_pit_rank_next_open_cash_and_spot_only_costs(self):
        result = self.run_spot()
        first = result["registry"][0]
        self.assertEqual(first["known_at"], self.days[0])
        self.assertEqual(first["members"], [f"{s}-USDT" for s in ("A", "B", "BTC", "C", "D", "E", "F", "G")])
        fills = result["equal_weight"]["fills"]
        initial = [r for r in fills if r["executed_at"] == self.days[1]]
        self.assertEqual(len(initial), 8)
        self.assertAlmostEqual(float(max(Decimal(x["notional"]) for x in initial)), 1249.375312343828)
        self.assertTrue(all(f["executed_at"] > f["known_at"] for f in fills))
        self.assertEqual(result["equal_weight"]["returns"].values[0], 0)
        self.assertEqual(result["equal_weight"]["funding_cashflow"], "0")
        (btc_fill,) = result["btc"]["fills"]
        qty, fill, fee = map(Decimal, (btc_fill["quantity"], btc_fill["execution_price"], btc_fill["fee"]))
        self.assertAlmostEqual(float(qty * fill + fee), 10000)
        self.assertAlmostEqual(float(fee / (qty * fill)), 0.0005)
        self.assertIn("BTC-USDT", result["btc"]["terminal_quantities"])

    def test_terminal_sale_requires_observed_next_open_and_charges_both_sides(self):
        when = self.days[-2]
        result = self.run_spot("stress", liquidation_known_at=when)
        purchase, sale = result["btc"]["fills"]
        self.assertGreater(Decimal(purchase["quantity"]), 0)
        self.assertEqual(Decimal(sale["quantity"]), Decimal(purchase["quantity"]).copy_negate())
        self.assertEqual(sale["executed_at"], self.days[-1])
        self.assertAlmostEqual(float(Decimal(sale["fee"]) / Decimal(sale["notional"])), 0.001)
        self.assertFalse(result["btc"]["terminal_quantities"])
        self.assertFalse(result["equal_weight"]["terminal_quantities"])
        del self.args["opens"][self.days[-1], "BTC-USDT"]
        self.assertEqual(self.run_spot(liquidation_known_at=when)["status"], "unavailable")

    def test_spot_missing_future_or_unverified_refuses(self):
        self.assertEqual(self.run_spot(origin="okx")["status"], "unavailable")
        self.assertEqual(
            self.run_spot(universe_verified=False)["reason"], "historical_global_universe_unknown"
        )
        key = self.days[1], "BTC-USDT"
        self.args["opens"][key] = SpotPrice(100, self.days[1] + 1)
        self.assertEqual(self.run_spot()["reason"], "missing_or_unavailable_spot_price")

    def test_lag_known_at_no_future_and_no_liquidity_floor_for_baseline(self):
        decision = self.days[0]
        rows = self.args["turnover"]["I-USDT-SWAP"]
        rows[decision + DAY - 1] = Turnover(decision + DAY - 1, True, 2_000_000_000)
        self.assertNotIn("I-USDT", self.run_spot()["registry"][0]["members"])
        rows[decision - 1] = Turnover(decision, True, 20_000_001)
        self.assertEqual(self.run_spot()["registry"][0]["members"][0], "I-USDT")
        rows[decision - 1] = Turnover(decision + 1, True, 20_000_001)
        self.assertEqual(self.run_spot()["reason"], "missing_confirmed_lagged_turnover")

    def test_only_verified_eligible_metadata_enters_rank(self):
        metadata = self.args["perps"]
        listing = metadata["A-USDT-SWAP"][0]
        metadata["A-USDT-SWAP"] += (
            replace(listing, known_at=self.days[0] + 1, effective_at=self.days[0], tradable=False),
        )
        result = self.run_spot()
        self.assertIn("A-USDT", result["registry"][0]["members"])
        self.assertNotIn("A-USDT", result["registry"][1]["members"])
        for symbol in ("A", "B", "C", "D", "E"):
            key = symbol + "-USDT-SWAP"
            metadata[key] = (replace(listing, continuous_since=self.days[0] - 179 * DAY),)
        self.assertEqual(self.run_spot()["reason"], "fewer_than_six_baseline_members")


class EpisodeTests(TestCase):
    def setUp(self):
        self.days = days(12)
        self.a, self.b = Leg("book", "A"), Leg("book", "B")

    def signal(self, signal_id, when, a=None, b=None, **kwargs):
        targets = {k: number(v) for k, v in ((self.a, a), (self.b, b)) if v is not None}
        return Signal(signal_id, self.days[when], targets, **kwargs)

    def count(self, signals, intents=None, calendar=None):
        if intents is None:
            intents = {"entry": EntryIntent("book", self.days[0], {"A": 1, "B": -1})}
        ledger, marks = mock_ledger(self.days, "base", signals)
        result = qualifying_round_trips(
            origin=ORIGIN,
            scenario="base",
            ledger=ledger,
            intents=intents,
            marks=marks,
            days=calendar or self.days,
        )
        self.assertEqual(result["status"], "synthetic_diagnostic", result)
        fills = [r for r in ledger if r["event"] in {"fill", "risk_fill"}]
        self.assertAlmostEqual(
            sum(float(e["fee"]) for e in result["episodes"]),
            sum(float(r["fee"]) for r in fills),
        )
        self.assertAlmostEqual(
            sum(float(e["funding_cashflow"]) for e in result["episodes"]),
            sum(float(r["cash_delta"]) for r in ledger if r["event"] == "funding"),
        )
        return result

    def test_partial_add_reduce_one_round_trip_after_all_legs_established(self):
        entry = self.signal("entry", 0, 1, -1, fill_fraction={self.b: number(".5")})
        partial = self.signal("complete", 2, b=-1)
        reduction = self.signal("reduce", 4, ".5", "-.5")
        final = self.signal("final", 6, 0, 0)
        result = self.count([entry, partial, reduction, final])
        (trade,) = result["episodes"]
        self.assertEqual(result["qualifying_count"], 1)
        self.assertEqual(trade["hedge_established_at"], self.days[3])
        self.assertEqual(len(trade["fill_parts"]), 7)

    def test_orphan_or_wrong_ratio_counts_zero_but_keeps_costs(self):
        orphan = self.signal("entry", 0, 1, -1, fill_fraction={self.b: number(0)})
        final = self.signal("final", 4, 0, 0)
        for entry in (orphan, self.signal("entry", 0, 1, "-.2")):
            result = self.count([entry, final])
            (trade,) = result["episodes"]
            self.assertEqual(result["qualifying_count"], 0)
            self.assertIn("unqualified", trade["status"])
            self.assertGreater(float(trade["fee"]), 0)

    def test_reversal_only_opens_new_episode_when_book_really_crosses_flat(self):
        signals = [
            self.signal("entry", 0, 1, -1),
            self.signal("a-flat", 2, 0),
            self.signal("reverse", 4, -1, 1),
            self.signal("final", 6, 0, 0),
        ]
        intents = {
            "entry": EntryIntent("book", self.days[0], {"A": 1, "B": -1}),
            "reverse": EntryIntent("book", self.days[4], {"A": -1, "B": 1}),
        }
        # B is filled first at reversal when A's opening is delayed one daily open.
        signals[2] = replace(signals[2], delay_steps={self.a: 1})
        signals[3] = self.signal("final", 7, 0, 0)
        result = self.count(signals, intents)
        first, second = result["episodes"]
        self.assertEqual(result["qualifying_count"], 2)
        self.assertEqual(first["flat_at"], second["first_fill"])
        self.assertEqual(first["fill_parts"][-1]["seq"], second["fill_parts"][0]["seq"])
        self.assertEqual(second["hedge_established_at"], self.days[6])
        signals = [signals[0], self.signal("reverse", 4, -1, 1), signals[3]]
        self.assertEqual(self.count(signals, intents)["qualifying_count"], 1)

    def test_split_overlap_and_terminal_open_preserve_cash_and_unrealized(self):
        closed = self.count(
            [self.signal("entry", 0, 1, -1), self.signal("final", 6, 0, 0)],
            calendar=self.days[2:],
        )
        self.assertEqual(closed["qualifying_count"], 0)
        self.assertEqual(closed["episodes"][0]["status"], "split_overlap")
        opened = self.count([self.signal("entry", 0, 1, -1)])
        self.assertEqual(opened["qualifying_count"], 0)
        self.assertEqual(opened["episodes"][0]["status"], "terminal_open")
        self.assertNotEqual(float(opened["episodes"][0]["terminal_unrealized_pnl"]), 0)

    def test_refuse_missing_intent_or_real_before_accounting(self):
        ledger, marks = mock_ledger(self.days, "base", [self.signal("entry", 0, 1, -1)])
        args = dict(scenario="base", ledger=ledger, intents={}, marks=marks, days=self.days)
        self.assertEqual(
            qualifying_round_trips(origin=ORIGIN, **args)["reason"], "missing_or_inconsistent_entry_intent"
        )
        self.assertEqual(
            qualifying_round_trips(origin="real", **args)["reason"], "real_episode_count_disabled"
        )


class BootstrapTests(TestCase):
    def test_paired_circular_28_day_blocks_and_undefined_resamples(self):
        # 28 days consumes the same set of returns on every circular rotation. A
        # longer path can sample only the flat region, making some Sharpes undefined.
        varied = path([0.002, -0.001] * 14)
        result = paired_bootstrap(
            origin=ORIGIN, eligible_days=varied.days, strategy=varied, btc=varied, equal_weight=path([0] * 28)
        )
        self.assertEqual((result["replicates"], result["seed"], result["block_days"]), (10000, 290411, 28))
        intervals = result["intervals"]
        self.assertEqual(intervals["strategy_minus_btc"]["percentile_2_5_97_5"], [0.0, 0.0])
        self.assertEqual(intervals["equal_weight.sharpe"]["undefined"], 10000)
        expected = (1.002 * 0.999) ** (365 / 2) - 1
        for actual in intervals["strategy.cagr"]["percentile_2_5_97_5"]:
            self.assertAlmostEqual(actual, expected)
        longer = path([0] * 56 + [0.01])
        args = dict(
            origin=ORIGIN, eligible_days=longer.days, strategy=longer, btc=longer, equal_weight=longer
        )
        a = paired_bootstrap(**args)
        self.assertEqual(a, paired_bootstrap(**args))
        self.assertGreater(a["intervals"]["strategy.sharpe"]["undefined"], 0)
        self.assertGreater(a["intervals"]["strategy.sharpe"]["defined"], 0)

    def test_gap_mismatch_and_short_window_refuse(self):
        p = path([0.001] * 27)
        args = dict(origin=ORIGIN, eligible_days=p.days, strategy=p, btc=p, equal_weight=p)
        self.assertEqual(paired_bootstrap(**args)["reason"], "fewer_than_28_aligned_daily_observations")
        self.assertEqual(paired_bootstrap(**(args | {"eligible_days": p.days[1:]}))["status"], "unavailable")
        self.assertEqual(paired_bootstrap(**(args | {"origin": "real"}))["status"], "unavailable")
        self.assertEqual(
            paired_bootstrap(**(args | {"eligible_days": p.days[:1] + p.days[2:]}))["reason"],
            "noncontiguous_eligible_dates",
        )


class JointTests(TestCase):
    def setup_args(self, n=365):
        calendar = days(n)
        return {
            "origin": ORIGIN,
            "eligible_days": calendar,
            "trips": {
                "status": "synthetic_diagnostic",
                "origin": ORIGIN,
                "scenario": "base",
                "eligible_days": calendar,
                "qualifying_count": 30,
                "episodes": [{"status": "qualifying_round_trip"} for _ in range(30)],
            },
            "base": {
                "strategy": path(([0.002, -0.001] * n)[:n]),
                "btc": path(([0.003, -0.004] * n)[:n]),
                "equal_weight": path(([0.002, -0.002] * n)[:n]),
            },
            "stress": {
                "strategy": path(([0.0019, -0.0011] * n)[:n], "stress"),
                "btc": path(([0.003, -0.004] * n)[:n], "stress"),
                "equal_weight": path(([0.002, -0.002] * n)[:n], "stress"),
            },
            "validation": mock_validation(),
            "selected_config": "primary",
            "proofs": {s: ScenarioEvidence(ORIGIN, s, True, True, True, 1.0) for s in ("base", "stress")},
        }

    def test_conjunctive_numerical_pass_never_real_evidence(self):
        result = joint_acceptance(**self.setup_args())
        self.assertTrue(all(result["gates"].values()))
        self.assertTrue(result["synthetic_numerical_gates_passed"])
        self.assertFalse(result["acceptance_passed"])
        self.assertIsNone(result["real_returns"])
        self.assertIsNone(result["executable_capacity"])

    def test_missing_dates_proof_and_real_refusal(self):
        self.assertEqual(
            joint_acceptance(**self.setup_args(364))["reason"], "fewer_than_365_eligible_test_days"
        )
        args = self.setup_args()
        args["proofs"]["base"] = replace(args["proofs"]["base"], funding_calendar_and_marks_complete=None)
        self.assertEqual(joint_acceptance(**args)["reason"], "missing_cost_risk_or_participation_evidence")
        self.assertEqual(
            joint_acceptance(**(args | {"origin": "real"}))["reason"],
            "real_acceptance_disabled_pending_independent_preregistration",
        )

    def test_zero_variance_neighbour_and_trade_count_cannot_pass(self):
        args = self.setup_args()
        args["trips"]["qualifying_count"] = 29
        args["trips"]["episodes"].pop()
        args["base"]["btc"] = path([0.001] * 365)
        args["validation"]["window28"]["net_cagr"] = -0.02
        args["validation"]["window112"]["net_cagr"] = -0.02
        args["selected_config"] = "returns"
        args["proofs"]["stress"] = replace(args["proofs"]["stress"], participation_limit_met=False)
        result = joint_acceptance(**args)
        self.assertEqual(result["validation_selection_issue"], "neighbour_robustness_failed")
        self.assertIsNone(result["metrics"]["base"]["btc"]["sharpe"])
        for gate in (
            "completed_relation_trades",
            "sharpe_at_least_both_spot_benchmarks",
            "validation_selection",
            "participation_limit",
        ):
            self.assertFalse(result["gates"][gate])
        self.assertFalse(result["synthetic_numerical_gates_passed"])


def test_ci_migration_checks_legacy_archive_and_active_file(tmp_path):
    import json
    from pathlib import Path

    import pytest

    from research.engine import Rejected
    from research.frozen_sources import LIVE_FREEZE, ORIGINALS, verify_historical_sources

    checkout = Path(__file__).resolve().parents[1]
    migration = json.loads((checkout / ORIGINALS).read_text())
    workflow = ".github/workflows/test.yml"
    original = migration[workflow]
    paths = (LIVE_FREEZE, ORIGINALS, "research/frozen_sources.py", workflow, original["archive"])
    for name in paths:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes((checkout / name).read_bytes())

    expected = {workflow: original["sha256"]}
    verify_historical_sources(tmp_path, expected)
    for name in (workflow, original["archive"]):
        (tmp_path / name).write_text("changed")
        with pytest.raises(Rejected, match="Frozen source changed") as error:
            verify_historical_sources(tmp_path, expected)
        assert error.value.code == "freeze_mismatch"
        (tmp_path / name).write_bytes((checkout / name).read_bytes())

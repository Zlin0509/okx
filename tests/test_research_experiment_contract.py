"""Standard-library mock contract tests, usable without statistical dependencies or market caches."""

import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from research.decision_view import Observed
from research.engine import Rejected, number
from research.execution_audit import read_log
from research.experiments.contract import Journal, protocol, real_preflight, select_validation
from research.experiments.universe import candidate_family, pool_at
from research.portfolio import DAY


def data():
    ts = 400 * DAY + DAY - 1
    symbols = tuple("ABCDEFGH")
    obs = {
        ts - i * DAY: Observed(
            ts - i * DAY, ts - i * DAY, MappingProxyType(dict.fromkeys(symbols, number(10)))
        )
        for i in range(513)
    }
    meta = {
        s: [
            {
                "known_at": -1000 * DAY,
                "effective_at": -1000 * DAY,
                "listing": -1000 * DAY,
                "continuous": -1000 * DAY,
                "verified": True,
                "tradable": True,
            }
        ]
        for s in symbols
    }
    vol = {
        s: {ts - i * DAY: {"volCcyQuote": "20000000", "available_at": ts - i * DAY} for i in range(1, 31)}
        for s in symbols
    }
    return ts, meta, vol, obs


def validation():
    return {
        c["id"]: {
            "origin": "synthetic_test",
            "stage": "validation",
            "status": "available",
            "net_cagr": 0.1,
            "net_sharpe": 1.2,
            "turnover": 2.0,
            "data_risk_pass": True,
        }
        for c in protocol()["configs"]
    }


class ExperimentContractTests(unittest.TestCase):
    def test_exact_six_and_registered_costs(self):
        p = protocol()
        self.assertEqual(len(p["configs"]), 6)
        self.assertEqual([c["window"] for c in p["configs"]], [56, 28, 112, 56, 56, 56])
        self.assertEqual(p["cost_scenarios"]["stress"], {"fee_bps": 10, "slippage_bps": 10})

    def test_real_hard_gate_even_when_local_claims_review(self):
        claimed = {k: {"reviewed": True} for k in protocol()["real_required_artifacts"]}
        self.assertEqual(real_preflight(claimed)["status"], "refused")
        self.assertIsNone(real_preflight()["real_returns"])

    def test_unknown_universe_or_listing_never_becomes_permission(self):
        ts, meta, vol, obs = data()
        self.assertEqual(pool_at(ts, meta, vol, obs, universe_verified=False)["status"], "unknown")
        meta["A"][0]["listing"] = None
        self.assertEqual(pool_at(ts, meta, vol, obs, universe_verified=True)["status"], "unknown")

    def test_lag_only_ranking_and_future_changes_do_not_change_pool(self):
        ts, meta, vol, obs = data()
        before = pool_at(ts, meta, vol, obs, universe_verified=True)
        for s in meta:
            vol[s][ts] = {"volCcyQuote": "999999999999", "available_at": ts}
            meta[s].append({"known_at": ts + 1, "effective_at": ts - DAY, "listing": None})
        self.assertEqual(before, pool_at(ts, meta, vol, obs, universe_verified=True))
        self.assertEqual(before["members"], list("ABCDEFGH"))

    def test_zero_low_missing_unavailable_turnover_and_missing_prices(self):
        for mode in ("zero", "low", "missing", "late", "missing_price"):
            with self.subTest(mode=mode):
                ts, meta, vol, obs = data()
                if mode in {"zero", "low"}:
                    for row in vol["A"].values():
                        row["volCcyQuote"] = "0" if mode == "zero" else "1"
                elif mode == "missing":
                    del vol["A"][ts - DAY]
                elif mode == "late":
                    vol["A"][ts - DAY]["available_at"] = ts + 1
                else:
                    del obs[ts - DAY]
                pool = pool_at(ts, meta, vol, obs, universe_verified=True)
                self.assertNotIn("A", pool["members"])
                self.assertIn("A", pool["excluded"])

    def test_historical_delisting_and_full_day_age_gate(self):
        ts, meta, vol, obs = data()
        meta["A"][0]["delisting"] = ts
        meta["B"][0]["listing"] = ts - 180 * DAY
        result = pool_at(ts, meta, vol, obs, universe_verified=True)
        self.assertNotIn("A", result["members"])
        self.assertNotIn("B", result["members"])
        self.assertEqual(result["status"], "eligible")

    def test_full_candidate_family_size_and_direction(self):
        family = candidate_family(list("ABCDEFGH"), ("A", "B"))
        self.assertEqual(len(family), 34)
        self.assertEqual(len({c["id"] for c in family}), 34)
        self.assertEqual(family[0]["symbols"], ("A", "B"))

    def test_selection_ties_primary_and_neighbours_required(self):
        rows = validation()
        self.assertEqual(select_validation(rows), "primary")
        rows["returns"]["net_sharpe"] = 100
        rows["window28"]["net_cagr"] = rows["window112"]["net_cagr"] = -0.1
        with self.assertRaises(Rejected):
            select_validation(rows)

    def test_selection_rejects_test_fields_missing_member_and_undefined(self):
        for mode in ("test", "missing", "nan", "real"):
            with self.subTest(mode=mode):
                rows = validation()
                if mode == "test":
                    rows["primary"]["test_sharpe"] = 10
                elif mode == "missing":
                    del rows["returns"]
                elif mode == "nan":
                    rows["primary"]["net_sharpe"] = float("nan")
                else:
                    rows["primary"]["origin"] = "real_market"
                with self.assertRaises(Rejected):
                    select_validation(rows)

    def test_local_budget_failure_retention_and_confirmation_one_use(self):
        with tempfile.TemporaryDirectory() as folder:
            j = Journal(Path(folder) / "experiments.jsonl")
            binding = dict.fromkeys(
                ("data_sha256", "family_sha256", "source_sha256", "dates_sha256"), "1" * 64
            )
            token = j.reserve("primary", "training_validation", binding, origin="synthetic_test")
            j.finish(token, {"reason": "mock_failure"}, "failed")
            with self.assertRaises(Rejected):
                j.reserve("primary", "training_validation", binding, origin="synthetic_test")
            with self.assertRaises(Rejected):
                j.reserve("primary", "confirmation", binding, origin="synthetic_test")
            j.select(validation())
            token = j.reserve("primary", "confirmation", binding, origin="synthetic_test")
            j.finish(token, {"reason": "mock_failure"}, "failed")
            with self.assertRaises(Rejected):
                j.reserve("primary", "confirmation", binding, origin="synthetic_test")
            self.assertEqual(sum(e.get("status") == "failed" for e in read_log(j.path)), 2)

    def test_unknown_config_real_or_unbound_journal_attempt_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            j = Journal(Path(folder) / "experiments.jsonl")
            for cfg, binding, origin in [
                ("seventh", {}, "synthetic_test"),
                ("primary", {}, "real_market"),
                ("primary", {}, "synthetic_test"),
            ]:
                with self.subTest(cfg=cfg, origin=origin), self.assertRaises(Rejected):
                    j.reserve(cfg, "training_validation", binding, origin=origin)

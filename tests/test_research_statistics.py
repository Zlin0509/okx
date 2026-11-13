"""Official-library and as-of family-gate tests, using synthetic data only."""

import copy
from dataclasses import replace
from types import MappingProxyType

import numpy as np
import pytest
from statsmodels.regression.linear_model import OLS
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.stattools import adfuller, coint

from research.engine import Rejected
from research.statistics.core import (
    DAY,
    Candidate,
    Eligibility,
    adjust,
    decide_family,
    dependencies,
    evaluate_family,
    qualified_models,
    window_data,
)
from research.statistics.fixtures import asof, changed_future, family, synthetic_series
from research.statistics.runner import compare, numerical_reference, reproduce


@pytest.fixture(scope="module")
def sample():
    series = synthetic_series()
    plan, eligibility = family()
    view = asof(series)
    result = evaluate_family(view, plan, eligibility, origin="synthetic_test")
    return series, plan, eligibility, view, result


def test_pinned_dependencies_and_official_EG_residual_distribution(sample):
    assert dependencies()["statsmodels"] == "0.14.6"
    _, plan, _, view, batch = sample
    c = plan.candidates[0]
    data, _ = window_data(view, c, plan)
    official = coint(
        data[:, 0], data[:, 1:], trend="c", method="aeg", maxlag=8, autolag=None, return_results=False
    )
    recorded = batch["analyses"][c.id]
    assert recorded["tests"]["pair:eg"]["statistic"] == pytest.approx(official[0], abs=1e-10)
    assert recorded["tests"]["pair:eg"]["pvalue"] == pytest.approx(official[1], rel=1e-10)
    residual = OLS(data[:, 0], np.column_stack((np.ones(len(data)), data[:, 1:]))).fit().resid
    ordinary = adfuller(residual, regression="n", maxlag=8, autolag=None)
    assert recorded["residual_adf_statistic_only"] == pytest.approx(official[0], abs=1e-10)
    assert not np.isclose(ordinary[1], official[1], rtol=1e-3, atol=1e-10)
    assert recorded["residual_adf_ordinary_pvalue_used"] is False


def test_known_relations_and_negative_cases_without_hiding_finite_sample_error(sample):
    _, _, _, _, batch = sample
    assert batch["status"] == "complete"
    assert batch["permits"]["pair"]["status"] == "qualified"
    assert batch["permits"]["basket"]["status"] == "qualified"
    for name in ("independent", "break", "stationary_levels"):
        assert batch["permits"][name]["status"] == "rejected"
    assert "relationship_change_detected" in batch["permits"]["break"]["reasons"]
    assert batch["analyses"]["pair"]["price_model"]["beta"][0] == pytest.approx(1.2, abs=0.02)
    assert batch["analyses"]["basket"]["price_model"]["beta"] == pytest.approx([0.8, 0.3], abs=0.02)
    # Prespecified random-walk residual realized a false positive. Do not retune the seed or p threshold.
    report = reproduce()
    assert report["false_positives"] == ["rw_residual"]
    assert report["batch"]["permits"]["rw_residual"]["status"] == "qualified"


def test_all_raw_tests_and_adjustments_are_official_and_families_complete(sample):
    _, plan, _, _, batch = sample
    eg = [batch["analyses"][c.id]["tests"][c.id + ":eg"]["pvalue"] for c in plan.candidates]
    for method, key in [("holm", "EG_Holm"), ("fdr_by", "EG_BY_diagnostic_not_license")]:
        reject, adjusted, *_ = multipletests(eg, method=method, alpha=0.05)
        for c, r, p in zip(plan.candidates, reject, adjusted, strict=True):
            row = batch["adjustments"][key][c.id + ":eg"]
            assert row["adjusted_p"] == pytest.approx(p, abs=1e-14)
            assert row["reject_null"] == bool(r)
    tests = {"a": {"pvalue": 0.01}, "b": {"pvalue": 0.03}, "c": {"pvalue": 0.2}}
    assert adjust(tests, list(tests))["b"]["adjusted_p"] == pytest.approx(0.06)


@pytest.mark.parametrize("change", ["candidate_missing", "test_missing", "candidate_extra"])
def test_missing_or_extra_family_members_refuse_instead_of_imputing_pvalues(sample, change):
    _, plan, eligibility, view, batch = sample
    analyses = copy.deepcopy(batch["analyses"])
    if change == "candidate_missing":
        analyses.pop("pair")
    elif change == "test_missing":
        analyses["pair"]["tests"].pop("pair:eg")
    else:
        analyses["extra"] = analyses["pair"]
    with pytest.raises(Rejected) as caught:
        decide_family(view, plan, analyses, eligibility, origin="synthetic_test")
    assert caught.value.code == "incomplete_test_family"


def test_duplicate_prespecified_direction_is_rejected(sample):
    _, plan, *_ = sample
    bad = replace(plan, candidates=plan.candidates + (Candidate("duplicate", plan.candidates[0].symbols),))
    with pytest.raises(Rejected):
        bad.validate()


@pytest.mark.parametrize("origin", [None, "okx_m1_snapshot", "real_market"])
def test_real_or_unspecified_origin_never_runs_a_statistical_test(sample, monkeypatch, origin):
    _, plan, eligibility, view, _ = sample
    monkeypatch.setattr("research.statistics.core.analyze", lambda *_: pytest.fail("Must not run statistics"))
    with pytest.raises(Rejected) as caught:
        evaluate_family(view, plan, eligibility, origin=origin)
    assert caught.value.code == "real_statistics_disabled"


@pytest.mark.parametrize(
    "metadata",
    [
        Eligibility(),
        Eligibility("verified", 10**20, "synthetic_metadata_fixture"),
        Eligibility("verified", 0, "current_instruments_only"),
    ],
)
def test_unknown_historical_eligibility_denies_even_if_statistics_pass(sample, metadata):
    _, plan, eligibility, view, batch = sample
    changed = {**eligibility, "pair": metadata}
    result = decide_family(view, plan, batch["analyses"], changed, origin="synthetic_test")
    assert "point_in_time_eligibility_unknown" in result["permits"]["pair"]["reasons"]
    with pytest.raises(Rejected):
        qualified_models(view, plan, result, "pair", metadata)


def test_short_or_missing_window_blocks_entire_family_without_fake_p(sample):
    series, plan, eligibility, view, _ = sample
    short = evaluate_family(asof(series, 100), plan, eligibility, origin="synthetic_test")
    assert short["status"] == "unavailable" and short["permits"] == {}
    assert not short["analyses"]
    missing = replace(
        view,
        observations=MappingProxyType({t: row for t, row in view.observations.items() if t != view.ts - DAY}),
    )
    result = evaluate_family(missing, plan, eligibility, origin="synthetic_test")
    assert result["status"] == "unavailable" and result["permits"] == {}


def test_unavailable_lagged_observation_and_current_day_never_enter_training(sample):
    _, plan, eligibility, view, batch = sample
    current = view.observations[view.ts]
    observations = {**view.observations, view.ts: replace(current, prices={s: 1e90 for s in current.prices})}
    same = evaluate_family(
        replace(view, observations=MappingProxyType(observations)), plan, eligibility, origin="synthetic_test"
    )
    assert same == batch
    last = view.ts - DAY
    observations[last] = replace(observations[last], available_at=view.ts + 1)
    refused = evaluate_family(
        replace(view, observations=MappingProxyType(observations)), plan, eligibility, origin="synthetic_test"
    )
    assert refused["status"] == "unavailable"


def test_future_data_changes_cannot_change_any_past_test_permit_or_model(sample):
    series, plan, eligibility, view, batch = sample
    modified = changed_future(series, view.ts)
    same = evaluate_family(asof(modified), plan, eligibility, origin="synthetic_test")
    assert same == batch
    assert qualified_models(asof(modified), plan, same, "pair", eligibility["pair"]) == qualified_models(
        view, plan, batch, "pair", eligibility["pair"]
    )


def test_price_and_relative_return_models_are_distinct_and_time_bound(sample):
    series, plan, eligibility, view, batch = sample
    price, ret = qualified_models(view, plan, batch, "pair", eligibility["pair"])
    assert price.train_end == view.ts - DAY
    assert ret.intercept != price.intercept and ret.beta != price.beta
    values = ret.residual(view.observations[view.ts].prices, view.observations[view.ts - DAY].prices)
    assert np.isfinite(price.zscore(view.observations[view.ts].prices))
    assert np.isfinite(values["standardized_return_residual"])
    with pytest.raises(Rejected):
        qualified_models(asof(series, 513), plan, batch, "pair", eligibility["pair"])


def test_qualification_evidence_and_training_fingerprints_are_bound(sample):
    _, plan, eligibility, view, batch = sample
    changed = copy.deepcopy(batch)
    changed["permits"]["pair"]["status"] = "rejected"
    with pytest.raises(Rejected) as caught:
        qualified_models(view, plan, changed, "pair", eligibility["pair"])
    assert caught.value.code == "family_mismatch"
    observations = dict(view.observations)
    t = view.ts - DAY
    row = observations[t]
    observations[t] = replace(row, prices={**row.prices, "Y": row.prices["Y"] * 2})
    with pytest.raises(Rejected) as caught:
        qualified_models(
            replace(view, observations=MappingProxyType(observations)),
            plan,
            batch,
            "pair",
            eligibility["pair"],
        )
    assert caught.value.code == "training_mismatch"


def test_changed_other_family_member_cannot_reuse_a_pair_permit(sample):
    _, plan, eligibility, view, batch = sample
    observations = dict(view.observations)
    t = view.ts - DAY
    row = observations[t]
    observations[t] = replace(row, prices={**row.prices, "INDEPENDENT": row.prices["INDEPENDENT"] * 2})
    changed = replace(view, observations=MappingProxyType(observations))
    with pytest.raises(Rejected) as caught:
        qualified_models(changed, plan, batch, "pair", eligibility["pair"])
    assert caught.value.code == "training_mismatch"
    with pytest.raises(Rejected):
        decide_family(changed, plan, batch["analyses"], eligibility, origin="synthetic_test")


def test_low_information_or_singular_data_never_generate_pvalues(sample):
    _, plan, eligibility, view, _ = sample
    observations = {
        t: replace(row, prices={s: 10 for s in row.prices}) for t, row in view.observations.items()
    }
    result = evaluate_family(
        replace(view, observations=MappingProxyType(observations)), plan, eligibility, origin="synthetic_test"
    )
    assert result["status"] == "unavailable" and result["permits"] == {}
    assert not result["analyses"]


def test_numerical_tolerance_never_relaxes_discrete_qualification():
    assert numerical_reference({"tuple": ("a", "b")}) == {"tuple": ["a", "b"]}
    compare({"p": 0.1 + 1e-10, "permit": False}, {"p": 0.1, "permit": False})
    with pytest.raises(Rejected):
        compare({"p": 0.1, "permit": True}, {"p": 0.1, "permit": False})
    with pytest.raises(Rejected):
        compare({"permit": 1}, {"permit": True})
    assert numerical_reference({"family_sha256": "platform_specific", "p": 0.1}) == {"p": 0.1}

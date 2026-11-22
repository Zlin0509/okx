"""P2 provenance and disclosure regression; fixed synthetic data only."""

import copy
import json
import math
import pickle
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from research.engine import Rejected
from research.statistics.core import adjust, config, digest, qualified_models
from research.statistics.disclosure import check_documents, documents, rw_disclosure
from research.statistics.fixtures import asof, family, synthetic_series
from research.statistics.runner import (
    compare,
    evaluation_notes,
    legacy_projection,
    reproduce,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def result():
    return reproduce()


@pytest.mark.parametrize("candidate", ["pair", "basket", "rw_residual"])
def test_detached_models_keep_labels_bindings_and_serialization(result, candidate):
    plan, eligibility = family()
    view = asof(synthetic_series())
    batch = result["batch"]
    models = qualified_models(view, plan, batch, candidate, eligibility[candidate])
    for model in models:
        tag = model.provenance
        assert tag.origin == "synthetic_test"
        assert tag.source == "synthetic_statistical_screen_only"
        assert tag.screen_only is True
        assert tag.allowed_use == "synthetic_calculation"
        assert "no real qualification" in tag.limitations
        assert tag.candidate_id == candidate and tag.decision_ts == view.ts
        assert tag.plan_sha256 == batch["plan_sha256"]
        assert tag.family_sha256 == batch["family_sha256"]
        assert tag.training_sha256 == batch["analyses"][candidate]["training_sha256"]
        encoded = json.loads(json.dumps(asdict(model)))
        assert encoded["provenance"] == asdict(tag)
        restored = pickle.loads(pickle.dumps(model))
        assert restored == model
        assert restored.provenance == tag
        with pytest.raises(FrozenInstanceError):
            model.provenance = None
        with pytest.raises(FrozenInstanceError):
            tag.origin = "real_market"
        assert "false_positive" not in encoded
        assert "generator_truth" not in json.dumps(encoded)


@pytest.mark.parametrize(
    "usage",
    [
        {"origin": "real_market"},
        {"origin": None},
        {"use": "trading"},
        {"use": "profit"},
        {"use": "deployment"},
    ],
)
def test_detached_models_reject_real_or_other_uses(result, usage):
    plan, eligibility = family()
    view = asof(synthetic_series())
    price, returns = qualified_models(view, plan, result["batch"], "rw_residual", eligibility["rw_residual"])
    prices = view.observations[view.ts].prices
    previous = view.observations[view.ts - 86400000].prices
    for model, args, method in [(price, (prices,), "zscore"), (returns, (prices, previous), "residual")]:
        restored = pickle.loads(pickle.dumps(model))
        with pytest.raises(Rejected) as caught:
            getattr(restored, method)(*args, **usage)
        assert caught.value.code == "real_model_use_disabled"


def test_legacy_statistics_permits_and_scalar_model_outputs_are_unchanged(result):
    legacy = json.loads((ROOT / "research/M3B_EXPECTED.json").read_text())
    actual = json.loads(json.dumps(legacy_projection(result)))
    for batch in (actual["batch"], legacy["batch"]):
        # Each digest binds its own exact bytes, which may differ between BLAS kernels.
        family_sha = batch.pop("family_sha256")
        assert digest(batch) == family_sha
    compare(actual, legacy)
    alpha = config()["alpha"]
    for candidate, analysis in legacy["batch"]["analyses"].items():
        for test_id, row in analysis["tests"].items():
            if ":level:" in test_id:
                observed = actual["batch"]["analyses"][candidate]["tests"][test_id]["pvalue"]
                assert (observed <= alpha) == (row["pvalue"] <= alpha), test_id
    assert result["false_positives"] == ["rw_residual"]
    assert result["batch"]["permits"]["rw_residual"]["status"] == "qualified"


def test_truth_is_only_post_screen_evaluation_and_cannot_change_permission_or_models(result):
    before = copy.deepcopy(result)
    truth = result["generator_truth_price_relation"]
    changed = evaluation_notes(result["batch"], {key: not value for key, value in truth.items()})
    assert changed != evaluation_notes(result["batch"], truth)
    assert result == before
    assert result["batch"]["permits"]["rw_residual"]["status"] == "qualified"
    assert result["models"] == before["models"]


def test_both_documents_use_exact_machine_values_and_detect_drift(result, tmp_path):
    expected = json.loads((ROOT / "research/M3B_EXPECTED_R2.json").read_text())
    check_documents(ROOT, expected)
    disclosure = rw_disclosure(expected)
    assert "`0.009662979064298902`" in disclosure
    assert "`0.028988937192896705`" in disclosure
    assert "`0.03551144806129846`" in disclosure
    assert "0.03865" not in disclosure
    compare(result["batch"]["adjustments"], expected["batch"]["adjustments"], "result/batch/adjustments")
    (tmp_path / "research").mkdir()
    for name, content in documents(expected).items():
        assert disclosure in content
        (tmp_path / "research" / name).write_text(content, encoding="utf-8")
    check_documents(tmp_path, expected)
    path = tmp_path / "research/M3B_REPORT_R2.md"
    path.write_text(path.read_text().replace("0.028988937192896705", "0.03865"))
    with pytest.raises(Rejected) as caught:
        check_documents(tmp_path, expected)
    assert caught.value.code == "disclosure_mismatch"


def test_numeric_tolerance_cannot_hide_a_threshold_crossing_or_missing_member():
    alpha = config()["alpha"]
    below = math.nextafter(alpha, 0.0)
    above = math.nextafter(alpha, 1.0)
    at = adjust({"eg": {"pvalue": alpha}}, ("eg",))
    on_same_side = adjust({"eg": {"pvalue": below}}, ("eg",))
    crossed = adjust({"eg": {"pvalue": above}}, ("eg",))
    assert at["eg"]["reject_null"] is True
    assert crossed["eg"]["reject_null"] is False
    compare(on_same_side, at)
    compare(crossed["eg"]["adjusted_p"], at["eg"]["adjusted_p"])
    with pytest.raises(Rejected) as error:
        compare(crossed, at)
    assert error.value.code == "discrete_mismatch"
    assert str(error.value) == "result/eg/reject_null"
    with pytest.raises(Rejected) as error:
        compare({}, at)
    assert error.value.code == "schema_mismatch"

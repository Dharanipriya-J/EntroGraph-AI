import math

import pytest

from app.config import RiskConfig
from app.metrics import (auc_from_scores, compute_behavior_metrics, hour_bucket, jensen_shannon_divergence,
                         js_between_counters, normalized_entropy, shannon_entropy)


def test_entropy_known_values():
    assert shannon_entropy([1, 1]) == pytest.approx(1.0)
    assert shannon_entropy([1, 1, 1, 1]) == pytest.approx(2.0)
    assert shannon_entropy({"a": 3, "b": 1}) == pytest.approx(0.811278, abs=1e-5)


@pytest.mark.parametrize("counts", [[], {}, [0, 0], [5], {"only": 9}])
def test_entropy_edge_cases_are_zero(counts):
    assert shannon_entropy(counts) == 0.0
    assert normalized_entropy(counts) == 0.0


def test_entropy_rejects_negative_and_nan():
    with pytest.raises(ValueError):
        shannon_entropy([1, -1])
    with pytest.raises(ValueError):
        shannon_entropy([1, float("nan")])


def test_normalized_entropy_fixed_universe():
    assert normalized_entropy([1, 1, 1, 1]) == pytest.approx(1.0)
    assert normalized_entropy([1, 1], n_categories=4) == pytest.approx(0.5)
    assert normalized_entropy([7], n_categories=10) == 0.0
    with pytest.raises(ValueError):
        normalized_entropy([1, 1, 1], n_categories=2)


def test_jsd_properties():
    assert jensen_shannon_divergence([1, 0], [0, 1]) == pytest.approx(1.0)
    assert jensen_shannon_divergence([3, 1], [3, 1]) == pytest.approx(0.0, abs=1e-12)
    a, b = [5, 2, 1], [1, 2, 5]
    assert jensen_shannon_divergence(a, b) == pytest.approx(jensen_shannon_divergence(b, a))
    assert 0 < jensen_shannon_divergence(a, b) < 1
    assert jensen_shannon_divergence([10, 20], [1, 2]) == pytest.approx(0.0, abs=1e-12)


def test_jsd_errors():
    with pytest.raises(ValueError):
        jensen_shannon_divergence([1, 2], [1])
    with pytest.raises(ValueError):
        jensen_shannon_divergence([0, 0], [1, 1])
    with pytest.raises(ValueError):
        jensen_shannon_divergence([], [])


def test_js_between_counters_aligns_labels():
    assert js_between_counters({"a": 4}, {"b": 4}) == pytest.approx(1.0)
    assert js_between_counters({"a": 2, "b": 2}, {"b": 2, "a": 2}) == pytest.approx(0.0, abs=1e-12)


def test_high_entropy_is_not_drift():
    cfg = RiskConfig()
    cats = ["books", "home", "fashion", "toys", "sports"]
    ts0 = 1_700_000_000
    ev = [{"category": cats[i % 5], "ts": ts0 + i * 3600 * 24, "type": "view"} for i in range(40)]
    m = compute_behavior_metrics(ev, [], [], ts0 + 41 * 86400, cfg)
    assert m.category_baseline.normalized_entropy > 0.6
    assert m.drift_composite is not None and m.drift_composite < 0.1


def test_insufficient_data_is_reported_not_scored():
    cfg = RiskConfig()
    m = compute_behavior_metrics([{"category": "books", "ts": 1_700_000_000, "type": "view"}], [], [], 1_700_000_100, cfg)
    assert m.status != "ok" and m.drift_composite is None and m.notes


def test_hour_bucket_and_auc():
    assert hour_bucket(0) == "night" and hour_bucket(13 * 3600) == "afternoon"
    assert auc_from_scores([0.9, 0.8, 0.1], [1, 1, 0]) == 1.0
    assert auc_from_scores([0.5, 0.5], [1, 0]) == 0.5
    assert auc_from_scores([1, 2], [1, 1]) is None
    assert math.isclose(auc_from_scores([0.2, 0.9, 0.1, 0.8], [1, 1, 0, 0]), 0.75)

from app.evaluation import result_hash, run_evaluation
from app.metrics import auc_from_scores


def test_evaluation_is_reproducible(store):
    a, b = run_evaluation(store), run_evaluation(store)
    assert a["result_hash"] == b["result_hash"] == result_hash(a)
    assert [m.get("per_case") for m in a["methods"].values()] == [m.get("per_case") for m in b["methods"].values()]


def test_evaluation_shape_and_expected_findings(store):
    r = run_evaluation(store)
    assert r["n_cases"] == 42 and r["n_positive"] == 8 and r["n_negative"] == 34
    m = r["methods"]
    # same cases and same threshold for every method
    assert all(len(v["per_case"]) == 42 for v in m.values() if "per_case" in v)
    assert m["naive_shared_infrastructure"]["false_positive_rate"] > m["graph_assisted_2_hops"]["false_positive_rate"]
    assert m["graph_assisted_2_hops"]["precision"] >= m["isolated_baseline"]["precision"]
    assert m["graph_assisted_2_hops"]["recall"] >= m["isolated_baseline"]["recall"]
    # ablation: dropping traversal removes the graph signal; depth matters for path discovery
    assert m["graph_no_traversal"]["recall"] < m["graph_assisted_2_hops"]["recall"]
    assert m["path_discovery_2_hop"]["rate"] > m["path_discovery_1_hop"]["rate"]
    assert r["memory"]["same_target_retrieval"]["correct"] == r["memory"]["same_target_retrieval"]["cases"]
    assert r["memory"]["related_entity_retrieval"]["correct"] == r["memory"]["related_entity_retrieval"]["cases"]
    assert r["conclusion_update"]["passed"]
    for v in m.values():
        if "tp" in v:
            assert v["tp"] + v["fp"] + v["fn"] + v["tn"] == 42
            assert 0 <= v["precision"] <= 1 and 0 <= v["recall"] <= 1


def test_evaluation_does_not_touch_live_memory(store):
    before = store.run("memory_list", {"limit": 50})
    run_evaluation(store)
    assert store.run("memory_list", {"limit": 50}) == before

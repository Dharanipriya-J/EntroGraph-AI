import pytest

from app.evidence import bfs_paths
from app.queries import QUERIES
from app.scenarios import cases
from app.store import INDEXES, QueryError


def hop(store):
    return lambda ids: store.run("customer_hop", {"frontier": ids})


def test_scenario_loaded_in_falkordb(store):
    counts = {r["label"]: r["count"] for r in store.run("graph_label_counts")}
    assert counts["Customer"] == 43 and counts["Event"] > 2000 and counts["Device"] > 40
    rels = {r["type"] for r in store.run("graph_rel_counts")}
    assert {"HAS_SESSION", "USED_DEVICE", "FROM_IP", "PLACED", "USED_COUPON"} <= rels


def test_schema_initialisation_is_idempotent(store):
    store.ensure_schema()
    store.ensure_schema()
    res = store._graph.query("CALL db.indexes()")
    indexed = {(row[0], p) for row in res.result_set for p in row[1]}
    assert set(INDEXES) <= indexed


def test_only_registered_queries_run(store):
    with pytest.raises(QueryError):
        store.run("MATCH (n) DETACH DELETE n")
    with pytest.raises(QueryError):
        store.run("does_not_exist")


def test_literal_bounds_are_validated(store):
    with pytest.raises(QueryError):
        store.run("customer_paths", {"cid": "A1", "limit": 5}, {"DEPTH": "4); MATCH (n) DETACH DELETE (n"})
    with pytest.raises(QueryError):
        store.run("customer_paths", {"cid": "A1", "limit": 5}, {"DEPTH": 999})
    with pytest.raises(QueryError):
        store.run("customer_paths", {"cid": "A1", "limit": 5})


def test_injection_text_is_just_data(store):
    evil = "x'}) DETACH DELETE (c) //"
    assert store.run("customer_get", {"cid": evil}) == []
    assert store.run("graph_label_counts")[0]["count"] > 0


def test_every_read_query_parses_and_runs(store):
    params = {"cid": "A1", "ids": ["A1", "B1"], "id": "INV-X", "limit": 5, "frontier": ["A1"], "seq": 0}
    for q in QUERIES.values():
        if not q.write:
            store.run(q.name, params, {k: 4 for k in q.literals})


def test_hop_traversal_finds_planted_relationship(store):
    best, _ = bfs_paths(hop(store), "A1", 1)
    assert best["B1"]["link_class"] == "private_device"
    assert "DEV-X" in best["B1"]["display"] and best["B1"]["customer_hops"] == 1


def test_two_hops_needed_for_chain(store):
    one, _ = bfs_paths(hop(store), "A3", 1)
    two, _ = bfs_paths(hop(store), "A3", 2)
    assert "C3" not in one and "C3" in two
    assert two["C3"]["customer_hops"] == 2 and two["C3"]["link_class"] == "private_device"
    assert "DEV-Y1" in two["C3"]["display"] and "DEV-Y2" in two["C3"]["display"]


def test_hop_traversal_agrees_with_variable_length_cypher(store):
    for cid in ("A1", "A3", "H01", "P06"):
        best, _ = bfs_paths(hop(store), cid, 2)
        raw = store.run("customer_paths", {"cid": cid, "limit": 5000}, {"DEPTH": 8})
        assert set(best) <= {r["other_id"] for r in raw}


def test_public_infrastructure_is_classified_public(store):
    for cid in ("P06", "K01"):
        best, _ = bfs_paths(hop(store), cid, 1)
        assert best and all(p["link_class"] == "public_shared" for p in best.values())


def test_shared_device_and_ip_queries(store):
    dev = store.run("shared_devices", {"cid": "A1"})
    assert {r["other_id"] for r in dev if r["device_id"] == "DEV-X"} == {"B1", "C1"}
    ips = store.run("shared_ips", {"cid": "P01"})
    assert any(r["ip_id"] == "IP-CAFE" and r["network_type"] == "public_wifi" for r in ips)
    hubs = {r["infra_id"]: r["customers"] for r in store.run("infra_hub_size", {"cid": "P01"})}
    assert hubs["IP-CAFE"] >= 6


def test_event_timeline_and_scope_comparison(store):
    ev = store.run("customer_events", {"cid": "A1"})
    assert [e["ts"] for e in ev] == sorted(e["ts"] for e in ev)
    sc = store.run("scope_comparison", {"cid": "A1"})[0]
    assert sc["reachable_customers_1hop"] == 2 and sc["shared_infra_nodes"] >= 1
    assert store.run("scope_comparison", {"cid": "N01"})[0]["reachable_customers_1hop"] == 0


def test_expected_counterparts_reachable_for_all_planted_cases(store):
    for c in cases():
        if c.expected_counterparts:
            best, _ = bfs_paths(hop(store), c.customer_id, 2)
            assert set(c.expected_counterparts) <= set(best), c.customer_id

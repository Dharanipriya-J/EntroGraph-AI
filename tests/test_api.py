import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(store, settings):
    return TestClient(create_app(settings, store))


def test_health_overview_config_tools(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["interpretation_mode"] == "DeterministicInterpreter"
    o = client.get("/api/overview").json()
    assert any(l["label"] == "Customer" and l["count"] == 43 for l in o["labels"]) and o["leaderboard"]
    assert client.get("/api/config").json()["risk_config"]["flag_threshold"] == 50.0
    assert len(client.get("/api/tools").json()["tools"]) == 8
    assert client.get("/").status_code == 200 and client.get("/static/app.js").status_code == 200


def test_investigate_and_memory_endpoints(client):
    r = client.post("/api/investigate", json={"customer_id": "A1"}).json()
    inv = r["investigation_id"]
    assert inv.startswith("INV-") and r["risk"]["band"] == "medium"
    lst = client.get("/api/investigations").json()["investigations"]
    assert [i["id"] for i in lst] == [inv]
    d = client.get(f"/api/investigations/{inv}").json()
    assert d["report"]["target"]["id"] == "A1" and d["findings"]
    assert client.get("/api/investigations?customer_id=A1").json()["investigations"][0]["id"] == inv
    assert client.get("/api/investigations/INV-99999").status_code == 404


def test_validation_and_errors(client):
    assert client.post("/api/investigate", json={"customer_id": "NOPE"}).status_code == 404
    assert client.post("/api/investigate", json={"customer_id": "bad id!"}).status_code == 422
    assert client.post("/api/investigate", json={"customer_id": "A1", "max_customer_hops": 9}).status_code == 422
    assert client.post("/api/events", json={"customer_id": "A1", "category": "nope", "device_id": "d", "ip_id": "i"}).status_code == 422
    assert client.post("/api/events", json={"customer_id": "ZZ", "category": "books", "device_id": "d", "ip_id": "i"}).status_code == 404
    assert client.post("/api/events", json={"customer_id": "A1", "category": "books", "device_id": "d", "ip_id": "i", "type": "purchase"}).status_code == 422
    assert client.get("/api/compare/NOPE").status_code == 404
    assert client.get("/api/graph/neighborhood?customer_id=A1&hops=9").status_code == 422
    assert client.get("/api/customers/NOPE/timeline").status_code == 404


def test_ingest_changes_graph_and_investigation(client):
    before = client.post("/api/investigate", json={"customer_id": "A1", "persist": False}).json()["risk"]["score"]
    ev = client.post("/api/challenge/new-event").json()
    assert ev["event_id"].startswith("E-LIVE-")
    assert client.post("/api/challenge/new-event").status_code == 409
    after = client.post("/api/investigate", json={"customer_id": "A1", "persist": False}).json()
    assert after["risk"]["score"] > before and "D1" in [p["other_id"] for p in after["graph_paths"]]


def test_graph_timeline_compare_endpoints(client):
    g = client.get("/api/graph/neighborhood?customer_id=A1&hops=2").json()
    labels = {n["label"] for n in g["nodes"]}
    assert {"Customer", "Session", "Device", "Coupon", "Investigation"} >= {"Customer", "Session", "Device"}
    assert {"Customer", "Session", "Device"} <= labels and g["edges"] and g["center"] == "Customer:A1"
    e2 = client.get("/api/graph/neighborhood?customer_id=E2&hops=1").json()
    assert "IP" in {n["label"] for n in e2["nodes"]}  # ring 2 is linked through a shared IP
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])
    t = client.get("/api/customers/A1/timeline").json()
    assert set(t["linked_customers"]) == {"B1", "C1"} and t["events"]
    c = client.get("/api/compare/A1").json()
    assert c["graph_assisted"]["flagged"] and c["scope"]["reachable_customers_1hop"] == 2
    assert client.get("/api/graph/raw-paths?customer_id=A1").json()["paths"]


def test_reset_and_in_process_restart_keep_memory(base_store, settings):
    from app.config import Settings
    from app.store import GraphStore
    from app.scenarios import load_scenario
    # use a private store so closing/reconnecting does not disturb the shared session fixture
    import tempfile
    d = tempfile.mkdtemp()
    s = Settings(db_mode="embedded", embedded_path=d + "/r.rdb", graph_name="restart_api")
    st = GraphStore(s)
    load_scenario(st)
    c = TestClient(create_app(s, st))
    inv = c.post("/api/investigate", json={"customer_id": "A1"}).json()["investigation_id"]
    r = c.post("/api/admin/restart").json()
    assert r["restarted"] and r["memory_investigations_found"] == 1
    assert c.get(f"/api/investigations/{inv}").status_code == 200
    summary = c.post("/api/scenario/reset").json()
    assert summary["customers"] == 43 and c.get("/api/investigations").json()["investigations"] == []
    st.close()

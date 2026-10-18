import json

import httpx
import pytest

from app.agent import ConclusionBeforeContext, InvestigationAgent
from app.config import RiskConfig
from app.interpreter import AnthropicInterpreter
from app.models import EventIn
from app.scenarios import demo_event
from app.tools import AgentTools, ToolError, tool_specs


def investigate(store, cid, **kw):
    return InvestigationAgent(store).investigate(cid, persist=kw.pop("persist", False), **kw)


def comps(r):
    return {c["name"]: c["points"] for c in r["risk"]["components"]}


def test_graph_context_is_retrieved_before_conclusion(store):
    r = investigate(store, "A1")
    names = [t["tool"] for t in r["tool_trace"]]
    assert names.index("traverse_relationships") < names.index("assess_risk (heuristic, no I/O)")
    assert names[:5] == ["get_customer_context", "calculate_behavior_metrics", "traverse_relationships",
                         "get_investigation_memory", "collect_evidence"]
    assert any(q["name"] == "customer_hop" for t in r["tool_trace"] for q in t["queries"])


def test_guard_refuses_conclusion_without_graph_context():
    with pytest.raises(ConclusionBeforeContext):
        InvestigationAgent._guard([{"tool": "get_customer_context"}])


def test_report_contains_required_sections(store):
    r = investigate(store, "A1")
    for key in ("target", "risk", "layers", "graph_paths", "evidence", "benign_explanations", "previous_investigations",
                "changes", "recommended_action", "provenance", "limitations"):
        assert key in r
    assert set(r["layers"]) == {"observed_facts", "computed_metrics", "heuristic_scores", "ai_interpretation"}
    assert set(r["evidence"]) == {"supporting", "contradicting", "missing"} and r["evidence"]["missing"]
    assert "not proof of fraud" in r["risk"]["not_proof_notice"]
    m = r["layers"]["computed_metrics"]
    assert m["drift"]["composite_js"] is not None and "normalized_entropy" in m["category_entropy"]["baseline"]
    assert {e["layer"] for k in r["evidence"].values() for e in k} <= {"observed_fact", "computed_metric", "heuristic_score"}


def test_planted_relationship_found_with_exact_path(store):
    r = investigate(store, "A1")
    p = next(p for p in r["graph_paths"] if p["other_id"] == "B1")
    assert p["link_class"] == "private_device" and "DEV-X" in p["display"] and p["customer_hops"] == 1
    assert [n["label"] for n in p["nodes"]] == ["Customer", "Session", "Device", "Session", "Customer"]
    assert r["risk"]["band"] == "medium"


def test_benign_household_member_not_flagged_despite_shared_device(store):
    r = investigate(store, "C1")
    assert any(p["other_id"] == "A1" for p in r["graph_paths"])
    assert not r["risk"]["flagged"] and r["risk"]["band"] == "low"
    assert any("days old" in e["text"] for e in r["evidence"]["contradicting"])


@pytest.mark.parametrize("cid", ["P06", "K01", "H01", "N01", "D01", "D03"])
def test_shared_or_unusual_but_legitimate_is_not_flagged(store, cid):
    assert not investigate(store, cid)["risk"]["flagged"]


def test_shared_ip_alone_never_reaches_high(store):
    for cid in ("P06", "P01", "K01"):
        r = investigate(store, cid)
        assert r["risk"]["band"] == "low"
        assert any("not counted" in e["text"] or "busy shared" in e["text"] for e in r["evidence"]["contradicting"])


def test_high_drift_alone_is_not_enough(store):
    r = investigate(store, "D01")
    assert comps(r)["behavioural_drift"] == 20.0 and r["risk"]["band"] == "low"


def test_chain_requires_two_hops_to_complete_the_cluster(store):
    one = investigate(store, "A3", max_customer_hops=1)
    two = investigate(store, "A3", max_customer_hops=2)
    assert comps(one)["coupon_cluster"] < comps(two)["coupon_cluster"] == 20.0
    assert {p["other_id"] for p in two["graph_paths"]} == {"B3", "C3"}


def test_new_event_changes_conclusion_and_explains_why(store):
    ag = InvestigationAgent(store)
    first = ag.investigate("A1", persist=True)
    assert first["risk"]["band"] == "medium" and first["changes"] is None
    AgentTools(store).ingest_event(EventIn(**demo_event()))
    second = ag.investigate("A1", persist=True)
    ch = second["changes"]
    assert second["risk"]["band"] == "high" and ch["conclusion_changed"] and ch["score_delta"] > 0
    assert ch["previous_investigation_id"] == first["investigation_id"]
    assert [e["customer_id"] for e in ch["new_events"]] == ["D1"]
    assert any(e["key"] == "linked:private_device:D1" for e in ch["evidence_added"])


def test_memory_links_related_investigations(store):
    ag = InvestigationAgent(store)
    a = ag.investigate("A1", persist=True)
    b = ag.investigate("B1", persist=True)
    related = {r["id"]: r for r in b["previous_investigations"]["related"]}
    assert a["investigation_id"] in related and "DEV-X" in related[a["investigation_id"]]["shared_entities"]
    findings = store.run("memory_findings", {"id": a["investigation_id"]})
    assert {f["kind"] for f in findings} == {"supporting", "contradicting", "missing"}


def test_ingest_validation_and_errors(store):
    t = AgentTools(store)
    with pytest.raises(ToolError) as e:
        t.ingest_event(EventIn(customer_id="NOPE", category="books", device_id="d", ip_id="i"))
    assert e.value.status == 404
    with pytest.raises(ToolError) as e:
        t.ingest_event(EventIn(customer_id="A1", category="books", device_id="d", ip_id="i", type="purchase"))
    assert e.value.status == 422
    s = store.run("customer_sessions", {"cid": "B1"})[0]["session_id"]
    with pytest.raises(ToolError) as e:
        t.ingest_event(EventIn(customer_id="A1", category="books", device_id="d", ip_id="i", session_id=s))
    assert e.value.status == 409
    with pytest.raises(ValueError):
        EventIn(customer_id="A1", category="weapons", device_id="d", ip_id="i")


def test_tool_registry_is_typed_and_complete():
    names = {t["name"] for t in tool_specs()}
    assert names == {"get_customer_context", "calculate_behavior_metrics", "traverse_relationships", "get_investigation_memory",
                     "collect_evidence", "save_investigation", "ingest_event", "compare_with_baseline"}
    assert all(t["input_schema"]["properties"] for t in tool_specs())


def test_deterministic_mode_is_labelled(store):
    ai = investigate(store, "A1")["layers"]["ai_interpretation"]
    assert ai["mode"] == "deterministic_template" and "no LLM" in ai["label"]


def test_llm_interpreter_only_changes_text(store):
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Plain-language summary."}]})

    base = investigate(store, "A1")
    ag = InvestigationAgent(store, interpreter=AnthropicInterpreter("k", "m", transport=httpx.MockTransport(handler)))
    r = ag.investigate("A1", persist=False)
    assert r["layers"]["ai_interpretation"]["mode"] == "llm" and r["risk"]["score"] == base["risk"]["score"]
    assert "MATCH" not in seen["body"]["messages"][0]["content"]  # the model is never asked for Cypher


def test_llm_failure_falls_back_to_deterministic(store):
    ag = InvestigationAgent(store, interpreter=AnthropicInterpreter("k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    r = ag.investigate("A1", persist=False)
    assert r["layers"]["ai_interpretation"]["mode"] == "deterministic_fallback" and r["risk"]["band"] == "medium"


def test_config_changes_score_transparently(store):
    base = investigate(store, "A1")
    other = InvestigationAgent(store, RiskConfig(w_coupon_cluster=0.0)).investigate("A1", persist=False)
    assert other["risk"]["score"] == pytest.approx(base["risk"]["score"] - 10.0)
    assert other["risk"]["config_version"] != base["risk"]["config_version"]

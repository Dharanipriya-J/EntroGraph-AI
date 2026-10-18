"""Comparison methods evaluated on exactly the same cases.

 * isolated      - behavioural components computed from the target's OWN events only (no traversal, no profile
                   context, no memory). Score = behavioural points scaled to 0-100.
 * naive_graph   - flags a customer if ANY device or IP is shared with another customer (no discounting).
 * graph_assisted - the full agent pipeline (traversal + evidence + mitigations + memory).
"""
from __future__ import annotations

import time
from dataclasses import replace

from .config import RiskConfig
from .risk import TargetFacts, assess, GraphFindings
from .store import GraphStore


def isolated_assessment(store: GraphStore, cid: str, cfg: RiskConfig) -> dict:
    from .tools import AgentTools
    t0 = time.perf_counter()
    tools = AgentTools(store, cfg)
    ctx = tools.get_customer_context(cid)
    m = tools.calculate_behavior_metrics(ctx)
    a = assess(m, None, TargetFacts(cid, 0, 0), cfg)
    return {"method": "isolated", "score": a.normalized_behavioural_score, "flagged": a.normalized_behavioural_score >= cfg.flag_threshold,
            "components": [c.__dict__ for c in a.components], "ms": round((time.perf_counter() - t0) * 1000, 2),
            "uses": "own events only"}


def naive_assessment(store: GraphStore, cid: str, cfg: RiskConfig) -> dict:
    t0 = time.perf_counter()
    devs = store.run("shared_devices", {"cid": cid})
    ips = store.run("shared_ips", {"cid": cid})
    others = sorted({r["other_id"] for r in devs + ips})
    return {"method": "naive_graph", "score": 100.0 if others else 0.0, "flagged": bool(others), "shares_with": others,
            "ms": round((time.perf_counter() - t0) * 1000, 2), "uses": "any shared device/IP, no discounting"}


def graph_assessment(store: GraphStore, cid: str, cfg: RiskConfig) -> dict:
    from .agent import InvestigationAgent
    t0 = time.perf_counter()
    rep = InvestigationAgent(store, cfg).investigate(cid, persist=False)
    return {"method": "graph_assisted", "score": rep["risk"]["score"], "flagged": rep["risk"]["flagged"], "band": rep["risk"]["band"],
            "paths": [p["other_id"] for p in rep["graph_paths"]], "ms": round((time.perf_counter() - t0) * 1000, 2),
            "uses": f"traversal ≤{cfg.max_customer_hops} customer hops, evidence, mitigations, memory"}


def compare_methods(store: GraphStore, cid: str, cfg: RiskConfig) -> dict:
    iso = isolated_assessment(store, cid, cfg)
    naive = naive_assessment(store, cid, cfg)
    graph = graph_assessment(store, cid, cfg)
    scope = store.run("scope_comparison", {"cid": cid})
    return {"customer_id": cid, "isolated": iso, "naive_graph": naive, "graph_assisted": graph,
            "scope": scope[0] if scope else {}, "flag_threshold": cfg.flag_threshold,
            "note": "Same data, same thresholds. Differences come only from the extra graph context each method is allowed to use."}

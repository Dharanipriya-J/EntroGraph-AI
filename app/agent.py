"""The graph-native investigation agent.

Fixed, auditable pipeline (the agent never decides risk before retrieving graph context):
  get_customer_context -> calculate_behavior_metrics -> traverse_relationships -> get_investigation_memory
  -> collect_evidence -> assess (heuristic) -> interpret (AI layer, text only) -> save_investigation
"""
from __future__ import annotations

import time
from dataclasses import asdict, replace

from .config import RiskConfig
from .interpreter import DeterministicInterpreter, Interpreter, NOT_PROOF
from .risk import assess
from .store import GraphStore
from .tools import AgentTools, ToolError

REQUIRED_BEFORE_CONCLUSION = ("get_customer_context", "traverse_relationships", "get_investigation_memory", "collect_evidence")

BAND_TEXT = {
    "low": "Low: no meaningful combination of signals. Ordinary behaviour is the most likely explanation.",
    "medium": "Medium: some signals are present but they can each be explained innocently. Worth a human look; not actionable on its own.",
    "high": "High: several independent signals agree (behaviour, graph links, coupon overlap). Prioritise for human review; still not proof.",
}
ACTIONS = {
    "low": "No action. Keep normal monitoring.",
    "medium": "Queue for manual review and gather the missing evidence (payment instruments, delivery addresses) before any decision.",
    "high": "Escalate to a human analyst. Consider step-up verification for the linked accounts. Do not auto-ban: confirm with the missing evidence first.",
}
LIMITATIONS = [
    "All data is synthetic and the planted scenarios were authored by the project author; results do not measure real-world fraud detection accuracy.",
    "Weights and thresholds are hand-set heuristics (see RiskConfig), not learned from labelled production data.",
    "The graph has no payment-instrument, address, login-failure, refund or chargeback entities.",
    "A shared device or IP is never treated as proof; public networks and kiosks are discounted and long-standing stable accounts are treated as benign links.",
    "Traversal is limited to the configured number of customer hops; connections beyond that are not examined.",
]


class ConclusionBeforeContext(RuntimeError):
    pass


class InvestigationAgent:
    def __init__(self, store: GraphStore, cfg: RiskConfig | None = None, interpreter: Interpreter | None = None):
        self.store = store
        self.cfg = cfg or RiskConfig()
        self.interpreter = interpreter or DeterministicInterpreter()

    # ------------------------------------------------------------------ pipeline
    def investigate(self, customer_id: str, persist: bool = True, max_customer_hops: int | None = None) -> dict:
        cfg = self.cfg if max_customer_hops is None else replace(self.cfg, max_customer_hops=max_customer_hops)
        tools = AgentTools(self.store, cfg)
        trace: list[dict] = []
        t_all = time.perf_counter()

        def step(tool: str, fn, summarize=lambda r: ""):
            pos, t0 = self.store.log_position(), time.perf_counter()
            out = fn()
            trace.append({"step": len(trace) + 1, "tool": tool, "ms": round((time.perf_counter() - t0) * 1000, 2),
                          "queries": [{"name": q["name"], "rows": q["rows"], "ms": q["ms"]} for q in self.store.log_since(pos)],
                          "summary": summarize(out)})
            return out

        graph_seq = self.store.current_seq()
        ctx = step("get_customer_context", lambda: tools.get_customer_context(customer_id),
                   lambda c: f"{len(c.sessions)} sessions, {len(c.events)} events, {len(c.orders)} orders")
        metrics = step("calculate_behavior_metrics", lambda: tools.calculate_behavior_metrics(ctx),
                       lambda m: f"status={m.status}, drift={m.drift_composite}")
        trav = step("traverse_relationships", lambda: tools.traverse_relationships(customer_id, cfg.max_customer_hops),
                    lambda t: f"enabled={t.enabled}, {len(t.paths)} reachable customer(s), {t.all_path_count} hop rows")
        entity_ids = sorted({customer_id} | {p["other_id"] for p in trav.paths}
                            | {n["id"] for p in trav.paths for n in p["nodes"] if n["label"] in ("Device", "IP")})
        memory = step("get_investigation_memory",
                      lambda: tools.get_investigation_memory(customer_id, entity_ids) if cfg.use_memory else tools.get_investigation_memory(customer_id, []),
                      lambda m: f"{len(m.same_target)} earlier for this target, {len(m.related)} about related entities")
        bundle = step("collect_evidence", lambda: tools.collect_evidence(ctx, metrics, trav, memory),
                      lambda b: f"{len(b.supporting)} supporting, {len(b.contradicting)} contradicting, {len(b.missing)} missing")
        self._guard(trace)
        t0 = time.perf_counter()
        assessment = assess(metrics, bundle.findings, bundle.facts, cfg)
        trace.append({"step": len(trace) + 1, "tool": "assess_risk (heuristic, no I/O)", "ms": round((time.perf_counter() - t0) * 1000, 2),
                      "queries": [], "summary": f"score={assessment.score}, band={assessment.band}"})

        report = self._compose(customer_id, ctx, metrics, trav, memory, bundle, assessment, cfg, graph_seq)
        report["changes"] = self._changes(tools, report, memory, bundle)
        t0 = time.perf_counter()
        report["layers"]["ai_interpretation"] = self.interpreter.interpret(report)
        trace.append({"step": len(trace) + 1, "tool": "interpret (AI layer; text only)", "ms": round((time.perf_counter() - t0) * 1000, 2),
                      "queries": [], "summary": report["layers"]["ai_interpretation"]["mode"]})
        report["conclusion"] = (f"{assessment.band} risk ({assessment.score:.0f}/100): "
                                + ("flagged for human review" if assessment.flagged else "not flagged") + "; not proof of fraud")
        report["tool_trace"] = trace
        if persist:
            involved = {
                "customers": sorted({customer_id} | {c.customer_id for c in bundle.findings.counterparts if c.link_class != "public_shared"}),
                "devices": sorted({n["id"] for p in trav.paths for n in p["nodes"] if n["label"] == "Device"}),
                "ips": sorted({n["id"] for p in trav.paths for n in p["nodes"] if n["label"] == "IP"}),
                "coupons": [bundle.findings.coupon_cluster_key[0]] if bundle.findings.coupon_cluster_key else [],
            }
            pos, t0 = self.store.log_position(), time.perf_counter()
            inv_id = tools.save_investigation(report, involved)
            trace.append({"step": len(trace) + 1, "tool": "save_investigation", "ms": round((time.perf_counter() - t0) * 1000, 2),
                          "queries": [{"name": q["name"], "rows": q["rows"], "ms": q["ms"]} for q in self.store.log_since(pos)],
                          "summary": f"stored as {inv_id}"})
        else:
            report["investigation_id"] = None
        report["total_ms"] = round((time.perf_counter() - t_all) * 1000, 2)
        return report

    @staticmethod
    def _guard(trace: list[dict]) -> None:
        done = {t["tool"] for t in trace}
        missing = [t for t in REQUIRED_BEFORE_CONCLUSION if t not in done]
        if missing:
            raise ConclusionBeforeContext(f"cannot conclude before running: {missing}")

    # ------------------------------------------------------------------ report assembly
    def _compose(self, cid, ctx, metrics, trav, memory, bundle, a, cfg, graph_seq) -> dict:
        ev = lambda items: [asdict(i) for i in items]
        f = bundle.findings
        facts_list = [asdict(i) for i in bundle.supporting + bundle.contradicting + bundle.missing if i.layer == "observed_fact"]
        computed = {
            "status": metrics.status, "baseline_events": metrics.baseline_events, "recent_events": metrics.recent_events,
            "category_entropy": {"baseline": asdict(metrics.category_baseline), "recent": asdict(metrics.category_recent)},
            "hour_entropy": {"baseline": asdict(metrics.hour_baseline), "recent": asdict(metrics.hour_recent)},
            "entropy_shift": metrics.entropy_shift,
            "drift": {"category_js": metrics.drift_category, "hour_js": metrics.drift_hour, "composite_js": metrics.drift_composite},
            "notes": metrics.notes,
            "formulae": "H=-Σp·log2 p; H_norm=H/log2(K); JSD(P,Q)=H((P+Q)/2)-(H(P)+H(Q))/2 (bits, 0..1)",
        }
        prev_same = [{k: r[k] for k in ("id", "seq", "created_at", "risk_score", "band", "conclusion")} for r in memory.same_target]
        paths = []
        for c in f.counterparts:
            paths.append({"other_id": c.customer_id, "name": c.name, "customer_hops": c.customer_hops, "rel_count": c.rel_count,
                          "link_class": c.link_class, "established_benign": c.established, "display": c.path_display,
                          "nodes": c.path_nodes, "rel_types": c.rel_types, "alt_paths": c.alt_paths, "tenure_days": c.tenure_days,
                          "drift": c.drift})
        sources: dict[str, int] = {}
        for e in ctx.events:
            sources[e.get("source") or "unknown"] = sources.get(e.get("source") or "unknown", 0) + 1
        return {
            "investigation_id": None, "created_at": None, "graph_seq": graph_seq,
            "target": {"entity_type": "Customer", "id": cid, "name": ctx.customer["name"], "segment": ctx.customer["segment"],
                       "tenure_days": round(bundle.facts.tenure_days, 1)},
            "mode": "graph_assisted", "max_customer_hops": cfg.max_customer_hops,
            "risk": {"score": a.score, "band": a.band, "flagged": a.flagged, "interpretation": BAND_TEXT[a.band],
                     "not_proof_notice": NOT_PROOF, "config_version": a.config_version,
                     "thresholds": {"medium": cfg.band_medium, "high": cfg.band_high, "flag": cfg.flag_threshold},
                     "components": [asdict(c) for c in a.components], "behavioural_points": a.behavioural_points,
                     "graph_points": a.graph_points, "mitigation_points": a.mitigation_points},
            "layers": {
                "observed_facts": facts_list, "computed_metrics": computed,
                "heuristic_scores": {"score": a.score, "band": a.band, "components": [asdict(c) for c in a.components]},
                "ai_interpretation": None,
            },
            "graph_paths": paths,
            "evidence": {"supporting": ev(bundle.supporting), "contradicting": ev(bundle.contradicting), "missing": ev(bundle.missing)},
            "benign_explanations": bundle.benign_explanations,
            "previous_investigations": {"same_target": prev_same, "related": memory.related},
            "changes": None,
            "recommended_action": ACTIONS[a.band],
            "provenance": {"graph": self.store.graph_name, "scenario": ctx.scenario, "event_sources": sources,
                           "risk_config": cfg.to_dict()},
            "limitations": LIMITATIONS,
            "conclusion": None, "tool_trace": [],
        }

    def _changes(self, tools: AgentTools, report: dict, memory, bundle) -> dict | None:
        if not memory.same_target:
            return None
        prev = memory.same_target[0]
        prev_rep = prev.get("report") or {}
        cids = sorted({report["target"]["id"]} | {c.customer_id for c in bundle.findings.counterparts}
                      | {x["other_id"] for x in prev_rep.get("graph_paths", [])})
        new_events = tools.store.run("events_since", {"ids": cids, "seq": prev.get("event_seq") or 0}) if prev.get("event_seq") is not None else []
        key = lambda r: {e["key"]: e for k in ("supporting", "contradicting", "missing") for e in r.get("evidence", {}).get(k, [])}
        before, after = key(prev_rep), report_keys(report)
        added = [{"key": k, "text": e["text"], "kind": e["kind"]} for k, e in after.items() if k not in before]
        removed = [{"key": k, "text": e["text"], "kind": e["kind"]} for k, e in before.items() if k not in after]
        delta = round(report["risk"]["score"] - float(prev["risk_score"]), 2)
        band_changed = prev["band"] != report["risk"]["band"]
        flag_before = bool(prev_rep.get("risk", {}).get("flagged", False))
        parts = [f"Score {prev['risk_score']:.0f} ({prev['band']}) → {report['risk']['score']:.0f} ({report['risk']['band']}), change {delta:+.1f}."]
        if new_events:
            parts.append(f"{len(new_events)} new event(s) since {prev['id']}: " + "; ".join(
                f"{e['customer_id']} {e['type']} {e['category'] or ''} (seq {e['seq']})" for e in new_events[:5]) + ".")
        else:
            parts.append(f"No new events since {prev['id']}; any difference comes from changed configuration or memory.")
        if added:
            parts.append("New evidence: " + " | ".join(a["text"][:140] for a in added[:3]))
        if removed:
            parts.append("Evidence no longer present: " + " | ".join(a["text"][:100] for a in removed[:2]))
        return {"previous_investigation_id": prev["id"], "previous_score": prev["risk_score"], "previous_band": prev["band"],
                "score_delta": delta, "band_changed": band_changed, "flag_changed": flag_before != report["risk"]["flagged"],
                "conclusion_changed": band_changed or flag_before != report["risk"]["flagged"],
                "new_events": new_events, "evidence_added": added, "evidence_removed": removed, "explanation": " ".join(parts)}


def report_keys(report: dict) -> dict:
    return {e["key"]: e for k in ("supporting", "contradicting", "missing") for e in report["evidence"][k]}

"""Deterministic evaluation: isolated baseline vs naive graph rule vs graph-assisted agent (+ ablations).

Runs on a dedicated evaluation graph (`<graph>_eval`) so it never touches the live investigation memory.
Every method sees exactly the same cases, the same data and the same flag threshold.
Results are about THIS synthetic, author-planted scenario, not about real-world fraud detection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from dataclasses import replace

from .agent import InvestigationAgent
from .baseline import isolated_assessment, naive_assessment
from .config import RiskConfig
from .metrics import auc_from_scores
from .scenarios import SCENARIO_ID, cases, demo_event, load_scenario
from .store import GraphStore
from .tools import AgentTools


def _confusion(flags: list[bool], labels: list[int]) -> dict:
    tp = sum(1 for f, y in zip(flags, labels) if f and y)
    fp = sum(1 for f, y in zip(flags, labels) if f and not y)
    fn = sum(1 for f, y in zip(flags, labels) if not f and y)
    tn = sum(1 for f, y in zip(flags, labels) if not f and not y)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
            "false_positive_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0}


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 2) if xs else 0.0


def run_evaluation(store: GraphStore, cfg: RiskConfig | None = None) -> dict:
    cfg = cfg or RiskConfig()
    ev = store.sibling(f"{store.graph_name}_eval")
    load_scenario(ev, reset=True)
    cs = cases()
    labels = [c.label for c in cs]

    methods: dict[str, dict] = {}

    def run_method(name: str, description: str, fn):
        scores, flags, lat, per_case = [], [], [], []
        for c in cs:
            t0 = time.perf_counter()
            out = fn(c.customer_id)
            ms = (time.perf_counter() - t0) * 1000
            scores.append(out["score"]); flags.append(bool(out["flagged"])); lat.append(ms)
            per_case.append({"customer_id": c.customer_id, "group": c.group, "label": c.label, "score": round(out["score"], 2), "flagged": bool(out["flagged"])})
        fp_groups: dict[str, int] = {}
        for pc in per_case:
            if pc["flagged"] and not pc["label"]:
                fp_groups[pc["group"]] = fp_groups.get(pc["group"], 0) + 1
        auc = auc_from_scores(scores, labels)
        methods[name] = {"description": description, **_confusion(flags, labels), "roc_auc": None if auc is None else round(auc, 4),
                         "false_positives_by_group": fp_groups, "latency_ms_mean": round(statistics.mean(lat), 2), "latency_ms_p95": _p95(lat),
                         "per_case": per_case}

    graph_cfg = lambda **kw: replace(cfg, **kw)
    run_method("isolated_baseline", "Target's own events only: entropy-shift, drift, new device, coupon (scaled 0-100). No traversal, no profile, no memory.",
               lambda cid: isolated_assessment(ev, cid, cfg))
    run_method("naive_shared_infrastructure", "Flag if ANY device or IP is shared with another customer (no discounting).",
               lambda cid: naive_assessment(ev, cid, cfg))
    for hops, nm in ((0, "graph_no_traversal"), (1, "graph_1_hop"), (2, "graph_assisted_2_hops")):
        c2 = graph_cfg(max_customer_hops=hops)
        run_method(nm, f"Graph-assisted agent, traversal limited to {hops} customer hop(s)" + (" (profile context and mitigations only)" if hops == 0 else ""),
                   lambda cid, c2=c2: _graph(ev, cid, c2))
    c3 = graph_cfg(max_customer_hops=2, use_mitigations=False)
    run_method("graph_2_hops_no_mitigations", "Ablation: 2 hops but the benign-context mitigations (tenure, household) are switched off.",
               lambda cid: _graph(ev, cid, c3))

    # ---- path discovery (does traversal reach the planted counterparts through a non-public link?)
    pos = [c for c in cs if c.expected_counterparts]
    path_rows = []
    for hops in (1, 2):
        ok = 0
        for c in pos:
            rep = InvestigationAgent(ev, graph_cfg(max_customer_hops=hops)).investigate(c.customer_id, persist=False)
            found = {p["other_id"] for p in rep["graph_paths"] if p["link_class"] != "public_shared"}
            hit = set(c.expected_counterparts) <= found
            ok += hit
            path_rows.append({"hops": hops, "customer_id": c.customer_id, "expected": list(c.expected_counterparts),
                              "found": sorted(found), "all_found": hit, "min_hops_needed": c.min_hops_needed})
        methods_key = f"path_discovery_{hops}_hop"
        methods[methods_key] = {"all_expected_counterparts_found": ok, "cases": len(pos), "rate": round(ok / len(pos), 4)}

    # ---- memory retrieval across sequential investigations (persisted in the eval graph)
    agent = InvestigationAgent(ev, cfg)
    saved: dict[str, str] = {}
    related_ok = related_total = 0
    for c in pos:
        rep = agent.investigate(c.customer_id, persist=True)
        saved[c.customer_id] = rep["investigation_id"]
        earlier = [x for x in c.expected_counterparts if x in saved and x != c.customer_id]
        if earlier:
            related_total += 1
            ids = {r["id"] for r in rep["previous_investigations"]["related"]}
            related_ok += any(saved[x] in ids for x in earlier)
    same_ok = 0
    for c in pos:
        rep2 = agent.investigate(c.customer_id, persist=True)
        same_ok += bool(rep2["changes"]) and rep2["changes"]["previous_investigation_id"] == saved[c.customer_id]
    memory = {"same_target_retrieval": {"correct": same_ok, "cases": len(pos)},
              "related_entity_retrieval": {"correct": related_ok, "cases": related_total}}

    # ---- conclusion update after a new event (Hidden Connection Challenge)
    load_scenario(ev, reset=True)
    ag2 = InvestigationAgent(ev, cfg)
    before = ag2.investigate("A1", persist=True)
    AgentTools(ev, cfg).ingest_event(_event_model())
    after = ag2.investigate("A1", persist=True)
    ch = after["changes"] or {}
    update = {"before": {"score": before["risk"]["score"], "band": before["risk"]["band"]},
              "after": {"score": after["risk"]["score"], "band": after["risk"]["band"]},
              "conclusion_changed": bool(ch.get("conclusion_changed")), "new_events_reported": len(ch.get("new_events", [])),
              "new_link_evidence": any(e["key"].startswith("linked:private_device:D1") for e in ch.get("evidence_added", [])),
              "expected": "band moves up (medium -> high) because dormant account D1 appears on device X with the same coupon",
              "passed": ch.get("conclusion_changed") is True and after["risk"]["score"] > before["risk"]["score"]}

    result = {"scenario": SCENARIO_ID, "n_cases": len(cs), "n_positive": sum(labels), "n_negative": len(labels) - sum(labels),
              "flag_threshold": cfg.flag_threshold, "config_version": cfg.version(), "methods": methods, "memory": memory,
              "conclusion_update": update, "path_discovery_rows": path_rows,
              "caveats": ["Synthetic data authored by the project author; planted rings and hard negatives are by construction.",
                          f"Only {sum(labels)} positive cases: metrics have wide uncertainty and must not be read as real-world accuracy.",
                          "Weights were set by hand and sanity-checked on this same scenario (in-sample)."]}
    result["result_hash"] = result_hash(result)
    return result


def _graph(ev: GraphStore, cid: str, cfg: RiskConfig) -> dict:
    rep = InvestigationAgent(ev, cfg).investigate(cid, persist=False)
    return {"score": rep["risk"]["score"], "flagged": rep["risk"]["flagged"]}


def _event_model():
    from .models import EventIn
    return EventIn(**demo_event())


def result_hash(result: dict) -> str:
    """Hash of everything except wall-clock timings, so two runs can be compared for reproducibility."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if not k.startswith("latency") and k not in ("result_hash",)}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o
    return hashlib.sha256(json.dumps(strip(result), sort_keys=True, default=str).encode()).hexdigest()[:16]


def group_flag_rates(result: dict):
    """pandas view: share of cases flagged per scenario group and method (shows WHERE each method goes wrong)."""
    import pandas as pd

    rows = []
    for name, m in result["methods"].items():
        for pc in m.get("per_case", []):
            rows.append({"method": name, "group": pc["group"], "label": pc["label"], "flagged": int(pc["flagged"]), "score": pc["score"],
                         "customer_id": pc["customer_id"]})
    df = pd.DataFrame(rows)
    return df, df.pivot_table(index="group", columns="method", values="flagged", aggfunc="mean").round(2)


if __name__ == "__main__":
    from .config import get_settings

    ap = argparse.ArgumentParser(description="Run the EntroGraph AI evaluation")
    ap.add_argument("--out", default="docs/evaluation_results.json")
    args = ap.parse_args()
    store = GraphStore(get_settings())
    res = run_evaluation(store)
    with open(args.out, "w") as fh:
        json.dump(res, fh, indent=2, default=str)
    for name, m in res["methods"].items():
        if "precision" in m:
            print(f"{name:32s} P={m['precision']:.2f} R={m['recall']:.2f} F1={m['f1']:.2f} FPR={m['false_positive_rate']:.2f} AUC={m['roc_auc']} "
                  f"TP={m['tp']} FP={m['fp']} FN={m['fn']} TN={m['tn']} mean={m['latency_ms_mean']}ms")
        else:
            print(f"{name:32s} {m['all_expected_counterparts_found']}/{m['cases']}")
    df, table = group_flag_rates(res)
    df.to_csv(args.out.replace(".json", "_per_case.csv"), index=False)
    table.to_csv(args.out.replace(".json", "_group_flag_rates.csv"))
    print("\nshare of cases flagged, by scenario group:\n" + table.to_string())
    print("memory:", res["memory"]); print("update:", res["conclusion_update"]); print("hash:", res["result_hash"])

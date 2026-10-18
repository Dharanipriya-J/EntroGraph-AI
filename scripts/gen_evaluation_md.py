"""Render EVALUATION.md from docs/evaluation_results.json (so the numbers in the document are the measured ones).

Usage: python -m app.evaluation --out docs/evaluation_results.json && python scripts/gen_evaluation_md.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
r = json.loads((ROOT / "docs" / "evaluation_results.json").read_text())
M = r["methods"]
order = ["isolated_baseline", "naive_shared_infrastructure", "graph_no_traversal", "graph_1_hop", "graph_assisted_2_hops", "graph_2_hops_no_mitigations"]
rows = "\n".join(
    f"| `{n}` | {M[n]['tp']} / {M[n]['fp']} / {M[n]['fn']} / {M[n]['tn']} | {M[n]['precision']:.2f} | {M[n]['recall']:.2f} | {M[n]['f1']:.2f} | "
    f"{M[n]['false_positive_rate']:.2f} | {M[n]['roc_auc']:.3f} | {M[n]['latency_ms_mean']} / {M[n]['latency_ms_p95']} |" for n in order)
fpg = "\n".join(f"- `{n}`: {M[n]['false_positives_by_group'] or 'none'}" for n in order)
u, mem = r["conclusion_update"], r["memory"]
doc = f"""# EVALUATION.md

> **Read this first.** All data is synthetic and the planted scenarios (rings, households, public Wi-Fi, kiosks, legitimate drift) were
> **authored by the project author**. These numbers show that the *mechanism works as designed on this data*. They are **not** an estimate of
> real-world fraud-detection accuracy. With only {r['n_positive']} positive cases, every rate has very wide uncertainty.

Reproduce: `python -m app.evaluation --out docs/evaluation_results.json` (or the **Evaluation** tab, or `POST /api/evaluation/run`).
Scenario `{r['scenario']}`, config version `{r['config_version']}`, result hash `{r['result_hash']}` (hash excludes timings; two runs give the same hash —
`tests/test_evaluation.py::test_evaluation_is_reproducible`).

## Methodology

- **Cases:** {r['n_cases']} customers ({r['n_positive']} planted coordinated-abuse accounts, {r['n_negative']} benign). The dormant account D1 is not a case (it has no activity until the live demo event).
- **Benign hard negatives:** 16 normal customers; 4 legitimate-drift customers (big category change, long history); 4 household members sharing a TV/laptop/IP; 6 customers on one café Wi-Fi; 3 library-kiosk users; 1 long-standing account (C1) that shares the *same device* as the planted ring.
- **Positives:** ring 1 (shared private phone + coupon, A1 & B1), ring 2 (three new accounts on one mobile-carrier IP + same coupon/category), ring 3 (chain A3–Y1–B3–Y2–C3; the cluster is only complete at two customer hops).
- **Methods, same data, same flag threshold ({r['flag_threshold']} on a 0–100 scale) for all:**
  - `isolated_baseline` – the target's own events only: entropy shift, JS drift, new device, coupon; behavioural points scaled to 0–100. No traversal, no profile context, no memory.
  - `naive_shared_infrastructure` – flag if *any* device/IP is shared with another customer.
  - `graph_*` – the full agent with traversal limited to 0 / 1 / 2 customer hops; plus an ablation with the benign-context mitigations switched off.
- **Metrics:** precision = TP/(TP+FP); recall = TP/(TP+FN); F1 = 2PR/(P+R); false-positive rate = FP/(FP+TN); ROC-AUC (rank based, uses scores not the threshold); latency per case (mean / p95, ms, embedded FalkorDB in the development sandbox).
- The evaluation runs on its own graph (`<graph>_eval`) and never writes to live investigation memory.

## Results (measured)

| Method | TP / FP / FN / TN | Precision | Recall | F1 | FPR | ROC-AUC | Latency ms (mean / p95) |
|---|---|---|---|---|---|---|---|
{rows}

False positives by scenario group:
{fpg}

Path discovery (did traversal reach every planted counterpart through a non-public link?): 1 hop **{M['path_discovery_1_hop']['all_expected_counterparts_found']}/{M['path_discovery_1_hop']['cases']}**, 2 hops **{M['path_discovery_2_hop']['all_expected_counterparts_found']}/{M['path_discovery_2_hop']['cases']}** (the ring-3 chain ends are two hops away).

Memory retrieval (investigations saved in sequence in the evaluation graph): same-target {mem['same_target_retrieval']['correct']}/{mem['same_target_retrieval']['cases']}, related-entity {mem['related_entity_retrieval']['correct']}/{mem['related_entity_retrieval']['cases']}.

Conclusion update after the new event: {u['before']['score']} ({u['before']['band']}) → {u['after']['score']} ({u['after']['band']}); conclusion changed = {u['conclusion_changed']}, new event reported = {u['new_events_reported']}, new linked-account evidence present = {u['new_link_evidence']}; passed = {u['passed']}.

## What the results do and do not show

- The **isolated baseline** catches every planted account on this data (its behavioural signal is deliberately moderate but present) but also flags all four legitimate-drift customers: unusual behaviour alone is not evidence.
- The **naive shared-infrastructure rule** flags every household, café, kiosk and the benign household member C1 – the false positives that motivate discounting public infrastructure.
- The **graph-assisted agent** separates these cases on this data. The perfect scores for the 1-hop and 2-hop variants are a property of how the scenario was built (the planted structure is exactly what the evidence rules look for); do not read them as expected real-world performance.
- **Ablations:** removing traversal (`graph_no_traversal`) removes the graph points; because behavioural points alone can reach only 40 of 100 they can never cross the {r['flag_threshold']} threshold, so it flags nothing (its ROC-AUC, which uses scores, is still high) — it is not directly comparable with `isolated_baseline`, which rescales. One hop is already enough to *classify* every case here, but **two hops are needed to discover all expected counterparts** (path discovery {M['path_discovery_1_hop']['all_expected_counterparts_found']}/{M['path_discovery_1_hop']['cases']} → {M['path_discovery_2_hop']['all_expected_counterparts_found']}/{M['path_discovery_2_hop']['cases']}) and to complete the ring-3 coupon cluster (unit test `test_chain_requires_two_hops_to_complete_the_cluster`). Switching the mitigations off changes scores of benign members but no flag decision on this data.
- Latency: the graph agent costs a few tens of milliseconds per investigation on this small graph (~3k nodes); no claim is made about large graphs.

## Caveats

{chr(10).join('- ' + c for c in r['caveats'])}
- Development history, for honesty: ring-1 behaviour was softened once after the first run so that the initial state is genuinely ambiguous (medium); risk weights were **not** changed after seeing evaluation results, but they were set by hand and sanity-checked on this same scenario (in-sample).
- Not executed in the development environment: tests against a real Docker FalkorDB server (see README "Testing").
"""
(ROOT / "EVALUATION.md").write_text(doc)
print("wrote EVALUATION.md")

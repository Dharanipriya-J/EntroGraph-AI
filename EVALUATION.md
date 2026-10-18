# EVALUATION.md

> **Read this first.** All data is synthetic and the planted scenarios (rings, households, public Wi-Fi, kiosks, legitimate drift) were
> **authored by the project author**. These numbers show that the *mechanism works as designed on this data*. They are **not** an estimate of
> real-world fraud-detection accuracy. With only 8 positive cases, every rate has very wide uncertainty.

Reproduce: `python -m app.evaluation --out docs/evaluation_results.json` (or the **Evaluation** tab, or `POST /api/evaluation/run`).
Scenario `default-v1`, config version `61c2f7b04c`, result hash `f4eaf71eb2b09e48` (hash excludes timings; two runs give the same hash —
`tests/test_evaluation.py::test_evaluation_is_reproducible`).

## Methodology

- **Cases:** 42 customers (8 planted coordinated-abuse accounts, 34 benign). The dormant account D1 is not a case (it has no activity until the live demo event).
- **Benign hard negatives:** 16 normal customers; 4 legitimate-drift customers (big category change, long history); 4 household members sharing a TV/laptop/IP; 6 customers on one café Wi-Fi; 3 library-kiosk users; 1 long-standing account (C1) that shares the *same device* as the planted ring.
- **Positives:** ring 1 (shared private phone + coupon, A1 & B1), ring 2 (three new accounts on one mobile-carrier IP + same coupon/category), ring 3 (chain A3–Y1–B3–Y2–C3; the cluster is only complete at two customer hops).
- **Methods, same data, same flag threshold (50.0 on a 0–100 scale) for all:**
  - `isolated_baseline` – the target's own events only: entropy shift, JS drift, new device, coupon; behavioural points scaled to 0–100. No traversal, no profile context, no memory.
  - `naive_shared_infrastructure` – flag if *any* device/IP is shared with another customer.
  - `graph_*` – the full agent with traversal limited to 0 / 1 / 2 customer hops; plus an ablation with the benign-context mitigations switched off.
- **Metrics:** precision = TP/(TP+FP); recall = TP/(TP+FN); F1 = 2PR/(P+R); false-positive rate = FP/(FP+TN); ROC-AUC (rank based, uses scores not the threshold); latency per case (mean / p95, ms, embedded FalkorDB in the development sandbox).
- The evaluation runs on its own graph (`<graph>_eval`) and never writes to live investigation memory.

## Results (measured)

| Method | TP / FP / FN / TN | Precision | Recall | F1 | FPR | ROC-AUC | Latency ms (mean / p95) |
|---|---|---|---|---|---|---|---|
| `isolated_baseline` | 8 / 4 / 0 / 30 | 0.67 | 1.00 | 0.80 | 0.12 | 0.978 | 4.21 / 5.04 |
| `naive_shared_infrastructure` | 8 / 14 / 0 / 20 | 0.36 | 1.00 | 0.53 | 0.41 | 0.794 | 1.06 / 1.53 |
| `graph_no_traversal` | 0 / 0 / 8 / 34 | 0.00 | 0.00 | 0.00 | 0.00 | 1.000 | 5.46 / 8.68 |
| `graph_1_hop` | 8 / 0 / 0 / 34 | 1.00 | 1.00 | 1.00 | 0.00 | 1.000 | 12.06 / 24.44 |
| `graph_assisted_2_hops` | 8 / 0 / 0 / 34 | 1.00 | 1.00 | 1.00 | 0.00 | 1.000 | 15.44 / 31.82 |
| `graph_2_hops_no_mitigations` | 8 / 0 / 0 / 34 | 1.00 | 1.00 | 1.00 | 0.00 | 1.000 | 15.9 / 33.6 |

False positives by scenario group:
- `isolated_baseline`: {'legit_drift': 4}
- `naive_shared_infrastructure`: {'household': 4, 'public_wifi': 6, 'public_terminal': 3, 'ring1_household': 1}
- `graph_no_traversal`: none
- `graph_1_hop`: none
- `graph_assisted_2_hops`: none
- `graph_2_hops_no_mitigations`: none

Path discovery (did traversal reach every planted counterpart through a non-public link?): 1 hop **6/8**, 2 hops **8/8** (the ring-3 chain ends are two hops away).

Memory retrieval (investigations saved in sequence in the evaluation graph): same-target 8/8, related-entity 5/5.

Conclusion update after the new event: 54.35 (medium) → 74.35 (high); conclusion changed = True, new event reported = 1, new linked-account evidence present = True; passed = True.

## What the results do and do not show

- The **isolated baseline** catches every planted account on this data (its behavioural signal is deliberately moderate but present) but also flags all four legitimate-drift customers: unusual behaviour alone is not evidence.
- The **naive shared-infrastructure rule** flags every household, café, kiosk and the benign household member C1 – the false positives that motivate discounting public infrastructure.
- The **graph-assisted agent** separates these cases on this data. The perfect scores for the 1-hop and 2-hop variants are a property of how the scenario was built (the planted structure is exactly what the evidence rules look for); do not read them as expected real-world performance.
- **Ablations:** removing traversal (`graph_no_traversal`) removes the graph points; because behavioural points alone can reach only 40 of 100 they can never cross the 50.0 threshold, so it flags nothing (its ROC-AUC, which uses scores, is still high) — it is not directly comparable with `isolated_baseline`, which rescales. One hop is already enough to *classify* every case here, but **two hops are needed to discover all expected counterparts** (path discovery 6/8 → 8/8) and to complete the ring-3 coupon cluster (unit test `test_chain_requires_two_hops_to_complete_the_cluster`). Switching the mitigations off changes scores of benign members but no flag decision on this data.
- Latency: the graph agent costs a few tens of milliseconds per investigation on this small graph (~3k nodes); no claim is made about large graphs.

## Caveats

- Synthetic data authored by the project author; planted rings and hard negatives are by construction.
- Only 8 positive cases: metrics have wide uncertainty and must not be read as real-world accuracy.
- Weights were set by hand and sanity-checked on this same scenario (in-sample).
- Development history, for honesty: ring-1 behaviour was softened once after the first run so that the initial state is genuinely ambiguous (medium); risk weights were **not** changed after seeing evaluation results, but they were set by hand and sanity-checked on this same scenario (in-sample).
- Not executed in the development environment: tests against a real Docker FalkorDB server (see README "Testing").

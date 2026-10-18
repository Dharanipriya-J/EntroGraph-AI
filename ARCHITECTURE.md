# ARCHITECTURE.md

```
Browser (HTML/CSS/JS + Cytoscape.js, vendored)
   │  JSON over HTTP
FastAPI (app/main.py) ──────────────── validation (pydantic), error mapping
   │
InvestigationAgent (app/agent.py) ── fixed, auditable pipeline
   │      uses typed tools (app/tools.py)           optional text-only interpreter (app/interpreter.py)
   │
GraphStore (app/store.py) ── runs ONLY queries registered in app/queries.py, parameterised
   │
FalkorDB (Docker container, or embedded falkordblite for local/tests)
```

## Modules

| Module | Responsibility |
|---|---|
| `metrics.py` | pure maths: Shannon entropy, normalised entropy, Jensen–Shannon divergence, drift, ROC-AUC |
| `risk.py` | transparent heuristic engine; every weight lives in `RiskConfig` (`config.py`) |
| `evidence.py` | hop-by-hop traversal (`bfs_paths`), path classification, graph findings, evidence text |
| `tools.py` | the 8 agent tools (typed inputs/outputs) + machine-readable registry (`GET /api/tools`) |
| `agent.py` | orchestration, report assembly, change detection against memory |
| `baseline.py` | isolated-event baseline, naive shared-infrastructure rule, comparison |
| `evaluation.py` | deterministic evaluation, ablations, reproducibility hash |
| `scenarios.py` | deterministic synthetic scenario + labelled cases |
| `queries.py` | the complete Cypher registry (source of `CYPHER_QUERIES.md`) |

## Agent pipeline (Track 01: agents that act on connected data)

1. `get_customer_context` – profile, sessions, events, orders from FalkorDB
2. `calculate_behavior_metrics` – entropy, normalised entropy, JS drift (baseline vs recent window)
3. `traverse_relationships` – `customer_hop` per hop (default 2 customer hops), shared device/IP, hub sizes
4. `get_investigation_memory` – earlier investigations of the target and of any entity found in step 3
5. `collect_evidence` – supporting / contradicting / missing evidence with provenance
6. `assess_risk` – heuristic points (behavioural + graph − mitigations); guard refuses to run before steps 1–5
7. `interpret` – deterministic template or optional LLM text; **never** changes the score, never writes Cypher
8. `save_investigation` – `Investigation`, `Finding`, `INVOLVES`, `SUPERSEDES` written to FalkorDB

The graph changes what the agent investigates (step 3 decides which accounts, devices and coupons are examined), which evidence it retrieves (step 5 queries only the linked accounts), how it updates conclusions (step 4 + change detection) and what it remembers (step 8).

## Four layers kept separate

| Layer | Where | Example |
|---|---|---|
| Observed facts | FalkorDB rows | path `A1 → S-0481 → DEV-X ← S-0490 ← B1` |
| Computed metrics | `metrics.py` | normalised entropy 0.62 → 0.71, JSD 0.18 bits |
| Heuristic scores | `risk.py` | `coupon_cluster +10 / 20` |
| AI interpretation | `interpreter.py` | prose summary, labelled with its mode |

## Safety design

- No free-form Cypher: unknown query names raise `QueryError`; values are parameters; the only formatted value is the integer path depth (validated 1..24).
- Shared device/IP is never proof: public Wi-Fi, carrier/corporate NAT and kiosks are discounted; long-standing stable accounts are treated as benign links; every report lists benign explanations and missing evidence.
- Deterministic mode is the default and is labelled in every report. The optional LLM (`ENTROGRAPH_LLM=anthropic`) only rewrites an already-computed summary and falls back on any error.

## Related work and what this adds (verified links)

| Source | What it does | Difference |
|---|---|---|
| Cheng et al., *Graph Neural Networks for Financial Fraud Detection: A Review* — https://arxiv.org/abs/2411.05815 | surveys 100+ GNN fraud systems on accounts/devices/IP graphs | learned models, no investigation memory, no per-case explanation of contradicting evidence |
| AI4Risk, *awesome-graph-based-fraud-detection* — https://github.com/AI4Risk/awesome-graph-based-fraud-detection | curated list of papers/code | research models, not an interactive agent |
| Rasmussen et al. (Zep), *A Temporal Knowledge Graph Architecture for Agent Memory* — https://arxiv.org/abs/2501.13956 (code: https://github.com/getzep/graphiti) | temporal knowledge-graph memory for conversational agents | conversation memory; EntroGraph stores structured *investigation* memory tied to fraud-domain entities |
| Tax et al., *Machine Learning for Fraud Detection in E-Commerce: A Research Agenda* — https://arxiv.org/abs/2107.01979 | notes that genuine drift also produces outliers, so not every outlier is fraud | motivates scoring drift only as one weak signal and requiring graph evidence |
| *Statistical Detection of Collective Data Fraud* — https://arxiv.org/abs/2001.00688 | divergence-based detection of collective anomalies | no graph traversal / agent / memory |
| *Variations of Neighbor Diversity for Fraudster Detection in Online Auction* — https://sciforum.net/paper/view/2637 | Shannon-entropy diversity of a user's neighbours as a fraud feature | entropy of neighbours, not of a customer's own behaviour over time |

**Specific contribution (not claimed to be unique or first):** an agent whose evidence collection is steered by FalkorDB traversal, that explicitly models *contradicting* graph evidence (public hubs, household members), persists its reasoning as queryable graph memory, and reports what changed after new events — with a same-data comparison against an isolated-event baseline. Prior work above covers the pieces separately; we did not find (in a limited search) one open project combining all of them, but the search was not exhaustive.

# EntroGraph AI

**Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB) · **Track 01 — Agents That Act on Connected Data** · Primary database: **FalkorDB**

> A graph-native investigation agent for synthetic e-commerce activity. It connects customers, sessions, devices, IPs, products, orders, coupons and its own past investigations in FalkorDB, then investigates unusual behaviour by traversing the graph, weighing supporting / contradicting / missing evidence, and remembering what it concluded.

| Item | Status |
|---|---|
| Public GitHub repository | **PENDING** (publish this folder, then add the link here) |
| Demo video | **PENDING** (record following `DEMO_SCRIPT.md`, then add the link here) |
| Live deployment | none — complete local setup below |
| Official rules | https://www.wemakedevs.org/hackathons/falkordb/rules |

## Problem statement

Rule systems judge one event at a time. A coordinated group of accounts can look ordinary event by event: each has mildly unusual behaviour, a coupon, a phone. The signal is in the *connections* (a shared device, the same coupon on the same category, accounts created together) — but shared devices and IPs are also everyday life (households, cafés, carrier NAT). The agent must find the connections **and** avoid treating them as proof.

## Differentiating contribution (not claimed to be unique or first)

1. FalkorDB steers the investigation: the traversal decides which accounts, devices and coupons the agent examines; evidence is collected only for what the graph links.
2. **Contradicting graph evidence is first-class**: public Wi-Fi, kiosks, carrier NAT and long-standing household members are discounted and shown as contradicting evidence.
3. **Persistent graph memory**: investigations, findings and the entities they involved are nodes/edges; later investigations retrieve them through shared entities and explain *what changed* after new events.
4. Four layers kept apart: observed facts, computed metrics, heuristic scores, AI interpretation.
5. Same-data comparison with an isolated-event baseline and a naive shared-infrastructure rule, plus ablations. Related work and differences: `ARCHITECTURE.md`.

## Quick start on macOS (VS Code)

Prerequisites: macOS, Python 3.12 (`brew install python@3.12`), Docker Desktop (for FalkorDB), VS Code.

```bash
# 1. get the code and open it
cd EntroGraph_AI
code .

# 2. virtual environment and dependencies
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt      # includes pytest and the embedded FalkorDB engine (falkordblite)

# 3. start FalkorDB (Docker)
docker run -d --name falkordb -p 6379:6379 -p 3000:3000 \
  -v falkordb_data:/var/lib/falkordb/data -e REDIS_ARGS="--appendonly yes" falkordb/falkordb:latest

# 4. configure and run the app
cp .env.example .env                       # defaults are fine
export FALKORDB_MODE=server FALKORDB_HOST=localhost FALKORDB_PORT=6379
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000** (API docs: http://localhost:8000/docs, FalkorDB Browser: http://localhost:3000). The synthetic scenario loads automatically the first time.

### Everything in Docker

```bash
cp .env.example .env
docker compose up --build        # api on :8000, FalkorDB on :6379, Browser on :3000
docker compose restart api       # restart the backend; memory stays in FalkorDB
docker compose restart falkordb  # restart the database; data is on the falkordb_data volume (AOF)
docker compose down              # stop (add -v to delete the data volume)
```

### No Docker (embedded FalkorDB engine)

```bash
pip install -r requirements-dev.txt
FALKORDB_MODE=embedded FALKORDB_EMBEDDED_PATH=./data/entrograph.rdb uvicorn app.main:app --port 8000
```
This runs a real FalkorDB engine inside Python (package `falkordblite`; macOS wheels exist for Intel and for Apple Silicon on macOS 15+). Data is stored in the given file, so memory survives stopping and restarting the server.

## Tests

```bash
pytest -q                                   # everything below, real FalkorDB engine (embedded)
FALKORDB_TEST_HOST=localhost pytest -q tests/test_persistence.py   # also run the same flow against your Docker FalkorDB
python -m app.evaluation --out docs/evaluation_results.json        # regenerate measured results
python scripts/gen_evaluation_md.py && python scripts/gen_cypher_docs.py   # regenerate EVALUATION.md / CYPHER_QUERIES.md
```
Covered: entropy/JSD/edge cases, graph init and every registered read query, multi-hop discovery, injection safety, restart persistence (including a real process kill and restart), ingestion + re-investigation, API validation and errors, evaluation reproducibility, docs in sync with code. Docker-specific behaviour is only exercised when `FALKORDB_TEST_HOST` is set.

## Architecture

See `ARCHITECTURE.md`. Short version: browser → FastAPI → `InvestigationAgent` (fixed pipeline of 8 typed tools) → `GraphStore` (only registered, parameterised Cypher) → FalkorDB. `GET /api/tools` lists the tools and their JSON schemas.

## Graph data model

Full detail in `GRAPH_SCHEMA.md`.

```
(Customer)-[:HAS_SESSION]->(Session)-[:USED_DEVICE]->(Device)      (Session)-[:FROM_IP]->(IP)
(Session)-[:HAS_EVENT]->(Event)      (Customer)-[:PLACED]->(Order)-[:CONTAINS]->(Product)
(Order)-[:USED_COUPON]->(Coupon)     (Order)-[:IN_SESSION]->(Session)
(Investigation)-[:TARGETS|INVOLVES]->(Customer|Device|IP|Coupon)   (Investigation)-[:HAS_FINDING]->(Finding)
(Investigation)-[:SUPERSEDES]->(Investigation)
```

## Cypher queries the product depends on

All 46 registered queries, each with purpose and why it matters, are in `CYPHER_QUERIES.md` (generated from `app/queries.py`). The key ones:

```cypher
// one customer-to-customer hop through a shared device or IP (the agent calls it once per hop)
MATCH (c:Customer)-[:HAS_SESSION]->(s:Session)-[:USED_DEVICE|FROM_IP]->(x)<-[:USED_DEVICE|FROM_IP]-(s2:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE c.id IN $frontier AND o.id <> c.id
RETURN c.id AS src, o.id AS dst, x.id AS infra_id, labels(x)[0] AS infra_label, x.kind AS kind, x.network_type AS network_type,
       min(s.id) AS src_session, min(s2.id) AS dst_session, count(DISTINCT s) AS src_sessions, count(DISTINCT s2) AS dst_sessions

// memory found through the graph: earlier investigations that involved any of these entities
MATCH (i:Investigation)-[:INVOLVES]->(e) WHERE e.id IN $ids AND i.target_id <> $cid
RETURN i.id, i.target_id, i.risk_score, i.band, collect(DISTINCT e.id) AS shared_entities ORDER BY i.seq DESC LIMIT $limit
```

## Metric formulas

- Shannon entropy (bits): `H = −Σ pᵢ log₂ pᵢ` (empty or single-category input → 0).
- Normalised entropy: `H / log₂(K)`, K = fixed universe (10 categories, 4 time-of-day buckets); K ≤ 1 → 0.
- Jensen–Shannon divergence: `JSD(P‖Q) = H(M) − ½(H(P)+H(Q))`, `M = ½(P+Q)`, base 2, range 0…1. Baseline = events older than 14 days, recent = last 14 days; composite drift = 0.7·category + 0.3·time-of-day.
- Risk score (0–100, `RiskConfig` in `app/config.py`, shown at `GET /api/config`): behavioural points (drift 20, entropy *shift* 10, new device 5, recent coupon 5) + graph points (linked accounts via private device 20, weak shared IP 5, coupon cluster 20, account-age proximity 10, memory escalation 5) − mitigations (established tenure 15, household context 10). Bands: low < 30 ≤ medium < 60 ≤ high. **High entropy alone is never scored.**

## Demo workflow

`DEMO_SCRIPT.md` — Hidden Connection Challenge: reset → investigate A1 → show path and evidence → baseline comparison → add new event → re-investigate (what changed) → restart → retrieve memory.

## API URLs

`GET /api/health`, `/api/overview`, `/api/config`, `/api/tools`, `/api/customers`, `/api/customers/{id}/context|timeline`, `POST /api/investigate`, `GET /api/compare/{id}`, `GET /api/investigations[/{id}]`, `GET /api/graph/neighborhood`, `/api/graph/raw-paths`, `POST /api/events`, `POST /api/scenario/reset`, `POST /api/admin/restart`, `GET /api/challenge/info`, `POST /api/challenge/new-event`, `POST /api/evaluation/run`, `GET /api/evaluation/latest`. Interactive docs at `/docs`.

## Evaluation results

Measured results and methodology: `EVALUATION.md` (regenerated from `docs/evaluation_results.json`). Headline on the synthetic scenario (42 cases, 8 planted positives, same threshold for all methods): isolated baseline F1 0.80 (4 false positives), naive shared-infrastructure rule F1 0.53 (14 false positives), graph-assisted agent F1 1.00 (0 false positives). These reflect the authored scenario and are **not real-world accuracy**.

## Limitations

- Synthetic data authored by the project author; results do not generalise. Only 8 positive cases.
- Hand-set heuristic weights; no learning from labelled data.
- No payment, address, login-failure, refund or chargeback entities in the graph.
- Traversal depth is bounded (default 2 customer hops); the hop-by-hop traversal is fast on this small graph, large graphs were not tested.
- Docker Compose files and the optional LLM mode were not executed in the development environment (no Docker daemon / API key there); the embedded FalkorDB engine and all tests were run.

## Responsible use

Synthetic data only (hackathon rule: public, synthetic or own data). The score is a prioritisation aid for **human review**, never proof of fraud and never grounds for automatic account action. A shared device or IP is not wrongdoing. Do not load personal or restricted data into the graph.

## AI assistance disclosure

This project was designed and generated with the help of an AI assistant (Claude by Anthropic) working from the participant's specification, and was tested by running the code. The participant is responsible for reviewing, understanding and being able to explain the graph model, queries, architecture and decisions (hackathon rules 11–13). The optional LLM mode in the application is off by default.

## License

MIT (see `LICENSE`). Cytoscape.js is MIT. FalkorDB itself is licensed separately by FalkorDB.

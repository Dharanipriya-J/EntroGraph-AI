# DEMO_SCRIPT.md — Hidden Connection Challenge (≈ 3 minutes)

Prepare (once): follow README "Quick start". Open http://localhost:8000 → tab **Hidden Connection Challenge**.

| Time | Click | Say |
|---|---|---|
| 0:00 | **1 · Reset & load** | "Synthetic e-commerce graph in FalkorDB: 43 customers, shared devices, public Wi-Fi, a library kiosk, households and three planted rings." |
| 0:20 | **2 · Investigate A1** | "Anika looks only mildly unusual. The agent retrieves graph context first, traverses two hops, and finds Bala (B1) through device DEV-X — see the exact path. It also finds Chandra (C1) on the same device and correctly treats her as a long-standing household member. Score ≈ 54, *medium*, flagged for review, not proof." Point at supporting / contradicting / **missing** evidence and the Cypher queries in the tool trace. |
| 1:00 | **3 · Compare baseline** | "Same customer, same threshold. The isolated baseline sees only her events. The naive rule flags every shared device — including the benign household member. The graph agent weighs the evidence." |
| 1:25 | **4 · Add new event** | "A dormant account, D1, now appears on DEV-X and redeems the same coupon on the same category." |
| 1:40 | **5 · Re-investigate** | "Score ≈ 74, *high*. The report explains what changed: the new event, the new linked account, the coupon cluster growing to three accounts." |
| 2:10 | **6 · Restart backend** | "This drops the connection and every cache and reconnects." |
| 2:25 | **7 · Retrieve memory** | "Both investigations come back from FalkorDB, with their findings." |
| 2:45 | Evaluation tab | "Same 42 cases for every method; here are real measured results and the caveats." |

## A true restart (stronger proof)

- Docker: `docker compose restart api` (API process restarts) and/or `docker compose restart falkordb` (database restarts; data is on the `falkordb_data` volume with AOF). Then open **Memory** — the investigations are still listed.
- No Docker (embedded): stop uvicorn with Ctrl-C, start it again with the same `FALKORDB_EMBEDDED_PATH`; **Memory** still lists them. `tests/test_persistence.py` automates exactly this with a real process kill.

## Likely judge questions

- *Does it still work without FalkorDB?* No: the path discovery, shared-infrastructure checks, coupon clusters among linked accounts and memory retrieval are all Cypher queries.
- *Is a shared IP fraud?* No — public Wi-Fi, kiosks and carrier NAT are discounted; households are mitigated; the report lists benign explanations.
- *Where is the LLM?* Optional text layer only; default is deterministic and labelled; no LLM-written Cypher exists.

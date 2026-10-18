# GRAPH_SCHEMA.md — the actual FalkorDB graph model

Graph name: `entrograph` (evaluation uses a separate graph `entrograph_eval` in the same database so it never touches live memory).
All data is **synthetic** (`source = "synthetic:default-v1"`) or ingested through the API (`source = "api:ingest"`).

```
(Customer)-[:HAS_SESSION]->(Session)-[:USED_DEVICE]->(Device)
                              |-[:FROM_IP]->(IP)
                              |-[:HAS_EVENT]->(Event)
(Customer)-[:PLACED]->(Order)-[:CONTAINS]->(Product)
                       |-[:IN_SESSION]->(Session)
                       |-[:USED_COUPON]->(Coupon)
(Investigation)-[:TARGETS]->(Customer)
(Investigation)-[:INVOLVES]->(Customer|Device|IP|Coupon)
(Investigation)-[:HAS_FINDING]->(Finding)
(Investigation)-[:SUPERSEDES]->(Investigation)        # newer -> older for the same target
```

The planted path of the Hidden Connection Challenge, as stored:
`(A1:Customer)-[:HAS_SESSION]->(S:Session)-[:USED_DEVICE]->(DEV-X:Device)<-[:USED_DEVICE]-(S2:Session)<-[:HAS_SESSION]-(B1:Customer)-[:PLACED]->(O:Order)-[:USED_COUPON]->(NEWUSER50:Coupon)`

## Node labels

| Label | Identifier | Properties | Notes |
|---|---|---|---|
| `Customer` | `id` (e.g. `A1`, `N01`) | `name`, `segment`, `created_at` (unix s), `region`, `source`, `seq` | `created_at` drives tenure and account-age proximity |
| `Session` | `id` (`S-0001`) | `started_at` (unix s), `customer_id`, `source`, `seq` | the bridge between customers and shared infrastructure |
| `Device` | `id` (`DEV-X`) | `kind` ∈ phone, laptop, tablet, smart_tv, kiosk; `label`, `source` | `kiosk` is a public terminal and is never a link |
| `IP` | `id` (`IP-CAFE`) | `network_type` ∈ residential, mobile_carrier, public_wifi, corporate_nat; `label`, `source` | public_wifi / corporate_nat are never a link |
| `Product` | `id` (`P-gift_cards`) | `name`, `category`, `price`, `source` | |
| `Order` | `id` (`O-0001`) | `placed_at` (unix s), `amount`, `customer_id`, `source`, `seq` | |
| `Coupon` | `id` (code, `NEWUSER50`) | `source` | |
| `Event` | `id` (`E-00001`) | `type` ∈ view, add_to_cart, purchase; `ts` (unix s), `category`, `product_id`, `customer_id`, `session_id`, `amount`, `seq`, `source` | `customer_id` is intentionally denormalised so timelines and entropy do not need a join |
| `Investigation` | `id` (`INV-00001`) | `seq`, `target_id`, `created_at` (ISO-8601 UTC), `risk_score`, `band`, `flagged`, `conclusion`, `config_version`, `interpretation_mode`, `event_seq`, `report_json`, `source='agent'` | the agent's persistent memory; `event_seq` = graph version the agent saw |
| `Finding` | `id` (`INV-00001-F03`) | `kind` ∈ supporting, contradicting, missing; `layer`, `key`, `text`, `provenance` (JSON) | one node per evidence item |
| `Meta` | `id` ∈ `clock`, `counter`, `investigation_counter` | `now_ts`, `scenario` / `seq` / `n` | scenario clock and monotonic counters (survive restarts) |

## Relationship types

| Type | Direction | Properties |
|---|---|---|
| `HAS_SESSION` | Customer → Session | `source` |
| `USED_DEVICE` | Session → Device | `source` |
| `FROM_IP` | Session → IP | `source` |
| `HAS_EVENT` | Session → Event | — |
| `PLACED` | Customer → Order | — |
| `CONTAINS` | Order → Product | — |
| `IN_SESSION` | Order → Session | — |
| `USED_COUPON` | Order → Coupon | — |
| `TARGETS` | Investigation → Customer | — |
| `INVOLVES` | Investigation → Customer / Device / IP / Coupon | — (the entities the investigation reasoned about; this is how memory is found through the graph) |
| `HAS_FINDING` | Investigation → Finding | — |
| `SUPERSEDES` | Investigation → Investigation | — |

## Identifiers, timestamps, provenance

- Every node has a string `id`; ids are unique per label and enforced by `MERGE` on an indexed property (range index per label/id).
- Times are **unix seconds, UTC** (`created_at`, `started_at`, `ts`, `placed_at`) so Cypher comparisons are numeric; `Investigation.created_at` is an ISO string for display only.
- Ordering that must be exact (what arrived since the last investigation?) uses the monotonic `seq` counter, not wall-clock time.
- Provenance: nodes and the session edges carry `source` (`synthetic:default-v1`, `api:ingest`, `agent`). Each evidence item in a report also records the query names that produced it.

## Indexes and constraints

`CREATE INDEX FOR (n:<Label>) ON (n.<prop>)` is issued for: Customer.id, Session.id, Device.id, IP.id, Product.id, Order.id, Coupon.id, Event.id, Event.customer_id, Meta.id, Investigation.id, Investigation.target_id, Finding.id (see `INDEXES` in `app/store.py`).
Unique constraints are **not** used: FalkorDB's `GRAPH.CONSTRAINT CREATE` needs a supporting index first and is a command, not Cypher; we rely on `MERGE` on indexed ids.

## Why the graph is not decoration

Take FalkorDB away and the agent cannot (a) find which other customers share a device or IP, (b) follow chains such as A3–DEV-Y1–B3–DEV-Y2–C3, (c) see which coupon redemptions belong to *linked* accounts, or (d) retrieve earlier investigations through shared entities. Those steps are Cypher queries (`customer_hop`, `shared_devices`, `coupon_overlap`, `memory_by_entities`).

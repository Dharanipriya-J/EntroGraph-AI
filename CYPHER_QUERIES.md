# CYPHER_QUERIES.md

Every Cypher statement the application can execute, generated from `app/queries.py` (`python scripts/gen_cypher_docs.py`).
A test (`tests/test_docs.py`) fails if this file is out of date.

**Safety rules**

- Queries are registered by name. `GraphStore.run(name, params)` refuses anything else, so neither the API nor an LLM can run free-form Cypher.
- Values are always passed as parameters (`$name`). The only text substitution is `__DEPTH__` in the variable-length query, which must be an integer in 1..24 (FalkorDB does not accept parameters for path-length bounds).
- Written against FalkorDB's documented Cypher subset, not Neo4j: no `shortestPath` in `MATCH` (FalkorDB only supports it in `WITH`/`RETURN` and not for undirected traversals), no Cypher `CREATE CONSTRAINT` (constraints use the `GRAPH.CONSTRAINT CREATE` command and need an index first; we use `MERGE` on indexed ids instead), `CREATE INDEX FOR (n:Label) ON (n.prop)` for range indexes, and `GRAPH.DELETE` (client `graph.delete()`) to drop a graph.
- Docs checked: https://docs.falkordb.com/cypher/ , https://docs.falkordb.com/commands/graph.constraint-create.html , https://docs.falkordb.com/cypher/cheat-sheet and the Python client https://github.com/FalkorDB/falkordb-py

**Indexes created at start-up** (idempotent): see `INDEXES` in `app/store.py`, each as `CREATE INDEX FOR (n:Label) ON (n.prop)`.

**Mapping to the required query types**

| Required | Query names |
|---|---|
| Entity / relationship creation or updates | `upsert_*`, `link_order_coupons` |
| Customer session retrieval | `customer_sessions`, `sessions_for_customers` |
| Multi-hop traversal | `customer_hop` (used by the agent), `customer_paths` (variable-length alternative) |
| Shared device / shared IP | `shared_devices`, `shared_ips`, `infra_hub_size` |
| Event timeline | `customer_events`, `events_for_customers`, `events_since` |
| Previous investigation retrieval | `memory_by_target`, `memory_by_entities`, `memory_get`, `memory_list`, `memory_chain` |
| Evidence persistence | `memory_create_investigation`, `memory_add_findings`, `memory_link_*`, `memory_supersede` |
| New-event ingestion | the same `upsert_*` queries with one row, plus `meta_next_seq`, `session_owner` |
| Baseline comparison | `scope_comparison` (+ `customer_events` / `customer_sessions` for the isolated view) |


## Entity creation and updates

### `upsert_customers` (write)

**Purpose:** Create or update Customer nodes in batch (idempotent MERGE on id).

**Why it matters to the agent:** Every investigation starts from a Customer node; MERGE keeps scenario loading and ingestion repeatable.

```cypher
UNWIND $rows AS r
MERGE (c:Customer {id: r.id})
SET c.name = r.name, c.segment = r.segment, c.created_at = r.created_at,
    c.region = r.region, c.source = r.source, c.seq = r.seq
```

### `upsert_devices` (write)

**Purpose:** Create or update Device nodes (kind decides whether sharing is meaningful).

**Why it matters to the agent:** Device kind (phone/laptop/tablet/smart_tv/kiosk) lets the agent discount public terminals.

```cypher
UNWIND $rows AS r
MERGE (d:Device {id: r.id})
ON CREATE SET d.kind = r.kind, d.label = r.label, d.source = r.source
```

### `upsert_ips` (write)

**Purpose:** Create or update IP nodes with a network_type (residential, mobile_carrier, public_wifi, corporate_nat).

**Why it matters to the agent:** Public and carrier-grade NAT addresses are shared by unrelated people; the type is what lets the agent avoid false alarms.

```cypher
UNWIND $rows AS r
MERGE (i:IP {id: r.id})
ON CREATE SET i.label = r.label, i.network_type = r.network_type, i.source = r.source
```

### `upsert_products` (write)

**Purpose:** Create or update Product nodes.

**Why it matters to the agent:** Products connect orders to categories, which is what the coupon-cluster evidence compares.

```cypher
UNWIND $rows AS r
MERGE (p:Product {id: r.id})
ON CREATE SET p.name = r.name, p.category = r.category, p.price = r.price, p.source = r.source
```

### `upsert_sessions` (write)

**Purpose:** Create Session nodes and connect them to Customer, Device and IP (provenance stored on each edge).

**Why it matters to the agent:** Sessions are the bridge between customers and shared infrastructure: Customer-Session-Device-Session-Customer is the core multi-hop path.

```cypher
UNWIND $rows AS r
MATCH (c:Customer {id: r.customer_id})
MATCH (d:Device {id: r.device_id})
MATCH (ip:IP {id: r.ip_id})
MERGE (s:Session {id: r.id})
ON CREATE SET s.started_at = r.started_at, s.customer_id = r.customer_id, s.source = r.source, s.seq = r.seq
MERGE (c)-[h:HAS_SESSION]->(s) ON CREATE SET h.source = r.source
MERGE (s)-[ud:USED_DEVICE]->(d) ON CREATE SET ud.source = r.source
MERGE (s)-[fi:FROM_IP]->(ip) ON CREATE SET fi.source = r.source
```

### `upsert_events` (write)

**Purpose:** Create Event nodes (view / add_to_cart / purchase) and attach them to their Session.

**Why it matters to the agent:** Events are the raw behavioural facts from which entropy and drift are computed.

```cypher
UNWIND $rows AS r
MATCH (s:Session {id: r.session_id})
MERGE (e:Event {id: r.id})
SET e.type = r.type, e.ts = r.ts, e.category = r.category, e.product_id = r.product_id,
    e.customer_id = r.customer_id, e.session_id = r.session_id, e.amount = r.amount,
    e.seq = r.seq, e.source = r.source
MERGE (s)-[:HAS_EVENT]->(e)
```

### `upsert_orders` (write)

**Purpose:** Create Order nodes linked to Customer, Session and Product.

**Why it matters to the agent:** Orders carry the coupon and product evidence used by the coupon-cluster check.

```cypher
UNWIND $rows AS r
MATCH (c:Customer {id: r.customer_id})
MATCH (p:Product {id: r.product_id})
MATCH (s:Session {id: r.session_id})
MERGE (o:Order {id: r.id})
SET o.placed_at = r.placed_at, o.amount = r.amount, o.customer_id = r.customer_id,
    o.source = r.source, o.seq = r.seq
MERGE (c)-[:PLACED]->(o)
MERGE (o)-[:CONTAINS]->(p)
MERGE (o)-[:IN_SESSION]->(s)
```

### `link_order_coupons` (write)

**Purpose:** Create Coupon nodes and connect the orders that redeemed them.

**Why it matters to the agent:** A coupon redeemed by several linked accounts on the same product category is a strong (but not conclusive) coordination signal.

```cypher
UNWIND $rows AS r
MATCH (o:Order {id: r.order_id})
MERGE (k:Coupon {id: r.coupon})
SET k.source = r.source
MERGE (o)-[:USED_COUPON]->(k)
```

## Customer session retrieval

### `session_owner`

**Purpose:** Return the customer that owns a session id (if it exists).

**Why it matters to the agent:** Ingestion refuses to attach an event to a session that belongs to a different customer.

```cypher
MATCH (s:Session {id: $id}) RETURN s.customer_id AS customer_id
```

### `customer_sessions`

**Purpose:** Retrieve a customer's sessions with the device and IP used in each.

**Why it matters to the agent:** Shows which devices/IPs the customer used and when; new-device detection and the first hop of every path start here.

```cypher
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(s:Session)
OPTIONAL MATCH (s)-[:USED_DEVICE]->(d:Device)
OPTIONAL MATCH (s)-[:FROM_IP]->(ip:IP)
RETURN s.id AS session_id, s.started_at AS started_at, d.id AS device_id, d.kind AS device_kind,
       ip.id AS ip_id, ip.network_type AS network_type
ORDER BY s.started_at
```

### `sessions_for_customers`

**Purpose:** Same as customer_sessions but for many customers at once.

**Why it matters to the agent:** Needed to compute new-device behaviour of linked accounts without one query per account.

```cypher
MATCH (c:Customer)-[:HAS_SESSION]->(s:Session) WHERE c.id IN $ids
OPTIONAL MATCH (s)-[:USED_DEVICE]->(d:Device)
OPTIONAL MATCH (s)-[:FROM_IP]->(ip:IP)
RETURN c.id AS customer_id, s.id AS session_id, s.started_at AS started_at, d.id AS device_id,
       d.kind AS device_kind, ip.id AS ip_id, ip.network_type AS network_type
ORDER BY s.started_at
```

## Meta and sequencing

### `meta_set_clock` (write)

**Purpose:** Store the scenario clock ('now') and scenario name.

**Why it matters to the agent:** Recent-versus-baseline windows are measured against this clock so results are deterministic.

```cypher
MERGE (m:Meta {id: 'clock'})
SET m.now_ts = $now_ts, m.scenario = $scenario
```

### `meta_get_clock`

**Purpose:** Read the scenario clock.

**Why it matters to the agent:** Every behavioural window depends on it.

```cypher
MATCH (m:Meta {id: 'clock'}) RETURN m.now_ts AS now_ts, m.scenario AS scenario
```

### `meta_next_seq` (write)

**Purpose:** Atomically increment and return a monotonic sequence number.

**Why it matters to the agent:** Sequence numbers order ingested events and investigations without relying on wall-clock time, so 'what changed since last time' is exact.

```cypher
MERGE (m:Meta {id: 'counter'})
ON CREATE SET m.seq = 0
SET m.seq = m.seq + 1
RETURN m.seq AS seq
```

### `meta_next_investigation_number` (write)

**Purpose:** Atomically increment and return the investigation counter.

**Why it matters to the agent:** Gives investigations short, readable, restart-safe ids (INV-00001, INV-00002, ...).

```cypher
MERGE (m:Meta {id: 'investigation_counter'})
ON CREATE SET m.n = 0
SET m.n = m.n + 1
RETURN m.n AS n
```

### `meta_current_seq`

**Purpose:** Read the current sequence number without changing it.

**Why it matters to the agent:** Used to stamp an investigation with the graph version it saw.

```cypher
MATCH (m:Meta {id: 'counter'}) RETURN m.seq AS seq
```

## Customer context retrieval

### `customer_get`

**Purpose:** Fetch one customer's profile.

**Why it matters to the agent:** Confirms the target exists and supplies account age for tenure and proximity checks.

```cypher
MATCH (c:Customer {id: $cid})
RETURN c.id AS id, c.name AS name, c.segment AS segment, c.created_at AS created_at, c.region AS region, c.source AS source
```

### `customers_by_ids`

**Purpose:** Fetch profiles for a list of customers.

**Why it matters to the agent:** Used to judge linked accounts (age, segment) in one round trip.

```cypher
MATCH (c:Customer) WHERE c.id IN $ids
RETURN c.id AS id, c.name AS name, c.segment AS segment, c.created_at AS created_at, c.source AS source
```

### `customers_list`

**Purpose:** List all customers.

**Why it matters to the agent:** Feeds the customer picker and the overview.

```cypher
MATCH (c:Customer)
RETURN c.id AS id, c.name AS name, c.segment AS segment, c.created_at AS created_at
ORDER BY c.id
```

### `orders_for_customers`

**Purpose:** Orders with product category and coupon, for one or many customers.

**Why it matters to the agent:** Provides order history (tenure evidence) and coupon redemptions (coordination evidence).

```cypher
MATCH (c:Customer)-[:PLACED]->(o:Order) WHERE c.id IN $ids
OPTIONAL MATCH (o)-[:CONTAINS]->(p:Product)
OPTIONAL MATCH (o)-[:USED_COUPON]->(k:Coupon)
RETURN c.id AS customer_id, o.id AS order_id, o.placed_at AS placed_at, o.amount AS amount,
       p.id AS product_id, p.category AS category, k.id AS coupon
ORDER BY o.placed_at
```

## Event timeline retrieval

### `customer_events`

**Purpose:** Retrieve all events of one customer in time order.

**Why it matters to the agent:** The raw material for entropy and drift; also the isolated-event baseline's only data source.

```cypher
MATCH (e:Event {customer_id: $cid})
RETURN e.id AS id, e.type AS type, e.ts AS ts, e.category AS category, e.product_id AS product_id,
       e.amount AS amount, e.session_id AS session_id, e.seq AS seq, e.source AS source
ORDER BY e.ts, e.id
```

### `events_for_customers`

**Purpose:** Retrieve events for many customers (linked accounts, merged timeline).

**Why it matters to the agent:** Lets the timeline view and the evidence collector see what linked accounts did and when.

```cypher
MATCH (e:Event) WHERE e.customer_id IN $ids
RETURN e.id AS id, e.customer_id AS customer_id, e.type AS type, e.ts AS ts, e.category AS category,
       e.product_id AS product_id, e.amount AS amount, e.session_id AS session_id, e.seq AS seq, e.source AS source
ORDER BY e.ts, e.id
```

### `events_since`

**Purpose:** Events with sequence number above a threshold for a set of customers.

**Why it matters to the agent:** Identifies exactly which new events arrived since the previous investigation, so the report can explain what changed.

```cypher
MATCH (e:Event) WHERE e.customer_id IN $ids AND e.seq > $seq
RETURN e.id AS id, e.customer_id AS customer_id, e.type AS type, e.ts AS ts, e.category AS category,
       e.session_id AS session_id, e.seq AS seq, e.source AS source
ORDER BY e.seq
```

## Multi-hop relationship traversal

### `customer_hop`

**Purpose:** ONE customer-to-customer hop: from a set of customers, find every other customer that shares a device or IP, returning one representative session pair per (customer, other customer, shared node). The agent calls this once per hop.

**Why it matters to the agent:** This is the core graph step. Aggregating per shared node keeps it fast (no session-pair explosion) while still returning the exact path Customer-Session-Device/IP-Session-Customer, with device kind / network type so public infrastructure can be discounted. Calling it again with the newly reached customers gives 2-hop and 3-hop paths.

```cypher
MATCH (c:Customer)-[:HAS_SESSION]->(s:Session)-[:USED_DEVICE|FROM_IP]->(x)<-[:USED_DEVICE|FROM_IP]-(s2:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE c.id IN $frontier AND o.id <> c.id
RETURN c.id AS src, o.id AS dst, x.id AS infra_id, labels(x)[0] AS infra_label, x.kind AS kind, x.network_type AS network_type,
       min(s.id) AS src_session, min(s2.id) AS dst_session, count(DISTINCT s) AS src_sessions, count(DISTINCT s2) AS dst_sessions
ORDER BY src, dst, infra_id
```

### `customer_paths`

**Purpose:** Variable-length alternative: every raw path from a customer to OTHER customers up to __DEPTH__ relationships (4 = one hop). Used for the explorer's 'raw paths' view and as an independent cross-check of the hop-by-hop traversal in the tests.

**Why it matters to the agent:** Shows the same multi-hop reachability with a single Cypher pattern. It enumerates every session combination, so it is slower on busy hubs; the agent therefore uses customer_hop for evidence.

```cypher
MATCH p = (c:Customer {id: $cid})-[:HAS_SESSION|USED_DEVICE|FROM_IP*2..__DEPTH__]-(o:Customer)
WHERE o.id <> $cid
RETURN o.id AS other_id, length(p) AS rel_count,
       [n IN nodes(p) | {id: n.id, label: labels(n)[0], kind: n.kind, network_type: n.network_type}] AS nodes,
       [r IN relationships(p) | type(r)] AS rel_types
ORDER BY rel_count
LIMIT $limit
```

### `coupon_overlap`

**Purpose:** Group orders by (coupon, product category) among a set of customers.

**Why it matters to the agent:** Finds accounts that redeemed the same coupon on the same category; combined with a shared-infrastructure path it becomes coordination evidence, on its own it is just a popular promotion.

```cypher
MATCH (c:Customer)-[:PLACED]->(o:Order)-[:USED_COUPON]->(k:Coupon)
WHERE c.id IN $ids
MATCH (o)-[:CONTAINS]->(p:Product)
RETURN k.id AS coupon, p.category AS category, c.id AS customer_id, o.id AS order_id, o.placed_at AS placed_at
ORDER BY placed_at
```

## Shared-device and shared-IP investigation

### `shared_devices`

**Purpose:** Devices used by the customer that are also used by other customers, with those customers and their session counts.

**Why it matters to the agent:** Answers 'who else touches my device?'. Device kind (kiosk vs phone) decides whether sharing is meaningful.

```cypher
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(s:Session)-[:USED_DEVICE]->(d:Device)
MATCH (d)<-[:USED_DEVICE]-(s2:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE o.id <> $cid
RETURN d.id AS device_id, d.kind AS kind, o.id AS other_id,
       count(DISTINCT s2) AS other_sessions, min(s2.started_at) AS first_seen, max(s2.started_at) AS last_seen
ORDER BY device_id, other_id
```

### `shared_ips`

**Purpose:** IP addresses used by the customer that are also used by other customers.

**Why it matters to the agent:** A shared IP is weak evidence: the network_type (public_wifi, corporate_nat, mobile_carrier, residential) tells the agent how much it can mean.

```cypher
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(s:Session)-[:FROM_IP]->(ip:IP)
MATCH (ip)<-[:FROM_IP]-(s2:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE o.id <> $cid
RETURN ip.id AS ip_id, ip.network_type AS network_type, o.id AS other_id,
       count(DISTINCT s2) AS other_sessions, min(s2.started_at) AS first_seen, max(s2.started_at) AS last_seen
ORDER BY ip_id, other_id
```

### `infra_hub_size`

**Purpose:** How many distinct customers use each device/IP the target used.

**Why it matters to the agent:** Large hubs (cafe Wi-Fi, carrier NAT) are explained by ordinary life, not coordination.

```cypher
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(:Session)-[:USED_DEVICE|FROM_IP]->(x)
MATCH (x)<-[:USED_DEVICE|FROM_IP]-(:Session)<-[:HAS_SESSION]-(o:Customer)
RETURN x.id AS infra_id, labels(x)[0] AS infra_label, count(DISTINCT o) AS customers
ORDER BY customers DESC
```

## Baseline comparison

### `scope_comparison`

**Purpose:** What an isolated-event view sees versus what one graph hop sees: counts of shared infrastructure nodes and reachable customers.

**Why it matters to the agent:** Quantifies, per case, how much extra context the graph provides compared with looking at one customer's own events.

```cypher
MATCH (c:Customer {id: $cid})
OPTIONAL MATCH (c)-[:HAS_SESSION]->(:Session)-[:USED_DEVICE|FROM_IP]->(x)<-[:USED_DEVICE|FROM_IP]-(:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE o.id <> $cid
RETURN c.id AS customer_id, count(DISTINCT x) AS shared_infra_nodes, count(DISTINCT o) AS reachable_customers_1hop
```

## Evidence persistence (investigation memory)

### `memory_create_investigation` (write)

**Purpose:** Persist an investigation as an Investigation node attached to its target Customer.

**Why it matters to the agent:** This is the agent's long-term memory: it survives backend and database restarts and is the starting point of the next investigation.

```cypher
MATCH (c:Customer {id: $target_id})
CREATE (i:Investigation {id: $id, seq: $seq, target_id: $target_id, created_at: $created_at,
                         risk_score: $risk_score, band: $band, flagged: $flagged, conclusion: $conclusion,
                         config_version: $config_version, interpretation_mode: $mode,
                         event_seq: $event_seq, report_json: $report_json, source: 'agent'})
CREATE (i)-[:TARGETS]->(c)
```

### `memory_add_findings` (write)

**Purpose:** Persist each piece of evidence as a Finding node linked to the investigation.

**Why it matters to the agent:** Findings are queryable individually (what supported or contradicted a past conclusion?) instead of being buried in text.

```cypher
MATCH (i:Investigation {id: $id})
UNWIND $rows AS r
CREATE (f:Finding {id: r.id, kind: r.kind, layer: r.layer, key: r.key, text: r.text, provenance: r.provenance})
CREATE (i)-[:HAS_FINDING]->(f)
```

### `memory_supersede` (write)

**Purpose:** Link a new investigation to the previous one for the same target.

**Why it matters to the agent:** Keeps an auditable chain of how a conclusion evolved.

```cypher
MATCH (n:Investigation {id: $new_id}), (o:Investigation {id: $old_id})
CREATE (n)-[:SUPERSEDES]->(o)
```

### `memory_link_customers` (write)

**Purpose:** Link an investigation to every Customer node it involved.

**Why it matters to the agent:** These INVOLVES edges are what later lets a different investigation discover this one through shared entities.

```cypher
MATCH (i:Investigation {id: $id})
UNWIND $ids AS x
MATCH (e:Customer {id: x})
MERGE (i)-[:INVOLVES]->(e)
```

### `memory_link_devices` (write)

**Purpose:** Link an investigation to every Device node it involved.

**Why it matters to the agent:** These INVOLVES edges are what later lets a different investigation discover this one through shared entities.

```cypher
MATCH (i:Investigation {id: $id})
UNWIND $ids AS x
MATCH (e:Device {id: x})
MERGE (i)-[:INVOLVES]->(e)
```

### `memory_link_ips` (write)

**Purpose:** Link an investigation to every IP node it involved.

**Why it matters to the agent:** These INVOLVES edges are what later lets a different investigation discover this one through shared entities.

```cypher
MATCH (i:Investigation {id: $id})
UNWIND $ids AS x
MATCH (e:IP {id: x})
MERGE (i)-[:INVOLVES]->(e)
```

### `memory_link_coupons` (write)

**Purpose:** Link an investigation to every Coupon node it involved.

**Why it matters to the agent:** These INVOLVES edges are what later lets a different investigation discover this one through shared entities.

```cypher
MATCH (i:Investigation {id: $id})
UNWIND $ids AS x
MATCH (e:Coupon {id: x})
MERGE (i)-[:INVOLVES]->(e)
```

## Previous investigation retrieval

### `memory_by_target`

**Purpose:** Fetch earlier investigations of the same target, newest first.

**Why it matters to the agent:** Lets the agent compare today's result with its own earlier conclusion (and detect what changed).

```cypher
MATCH (i:Investigation {target_id: $cid})
RETURN i.id AS id, i.seq AS seq, i.created_at AS created_at, i.risk_score AS risk_score, i.band AS band,
       i.flagged AS flagged, i.conclusion AS conclusion, i.event_seq AS event_seq, i.report_json AS report_json
ORDER BY i.seq DESC
LIMIT $limit
```

### `memory_by_entities`

**Purpose:** Fetch investigations about OTHER targets that involved any of the given entities.

**Why it matters to the agent:** Graph-native memory: a past finding about a linked account or a shared device is retrieved through the graph, not by keyword.

```cypher
MATCH (i:Investigation)-[:INVOLVES]->(e)
WHERE e.id IN $ids AND i.target_id <> $cid
RETURN i.id AS id, i.target_id AS target_id, i.seq AS seq, i.risk_score AS risk_score, i.band AS band,
       i.conclusion AS conclusion, collect(DISTINCT e.id) AS shared_entities
ORDER BY seq DESC
LIMIT $limit
```

### `memory_get`

**Purpose:** Fetch one investigation by id with its full stored report.

**Why it matters to the agent:** Used by the memory screen and the API.

```cypher
MATCH (i:Investigation {id: $id})
RETURN i.id AS id, i.seq AS seq, i.target_id AS target_id, i.created_at AS created_at, i.risk_score AS risk_score,
       i.band AS band, i.conclusion AS conclusion, i.event_seq AS event_seq, i.report_json AS report_json
```

### `memory_list`

**Purpose:** List all stored investigations (without the large JSON blob).

**Why it matters to the agent:** Powers the Persistent Memory screen.

```cypher
MATCH (i:Investigation)
RETURN i.id AS id, i.seq AS seq, i.target_id AS target_id, i.created_at AS created_at, i.risk_score AS risk_score,
       i.band AS band, i.conclusion AS conclusion, i.event_seq AS event_seq
ORDER BY i.seq DESC
LIMIT $limit
```

### `memory_findings`

**Purpose:** List the Finding nodes of one investigation.

**Why it matters to the agent:** Shows persisted evidence items and their provenance.

```cypher
MATCH (i:Investigation {id: $id})-[:HAS_FINDING]->(f:Finding)
RETURN f.id AS id, f.kind AS kind, f.layer AS layer, f.key AS key, f.text AS text, f.provenance AS provenance
ORDER BY f.id
```

### `memory_chain`

**Purpose:** Walk the SUPERSEDES chain from an investigation back in time.

**Why it matters to the agent:** Audit trail of how conclusions changed.

```cypher
MATCH (i:Investigation {id: $id})-[:SUPERSEDES*1..20]->(o:Investigation)
RETURN o.id AS id, o.risk_score AS risk_score, o.band AS band, o.seq AS seq
ORDER BY seq DESC
```

## Overview

### `graph_label_counts`

**Purpose:** Count nodes per label.

**Why it matters to the agent:** Proves to the user (and judges) that the displayed numbers come from FalkorDB.

```cypher
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC
```

### `graph_rel_counts`

**Purpose:** Count relationships per type.

**Why it matters to the agent:** Same purpose as graph_label_counts for edges.

```cypher
MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count ORDER BY count DESC
```

## Graph explorer

### `explorer_memory`

**Purpose:** Investigations (memory nodes) that involve or target any of the displayed entities.

**Why it matters to the agent:** Lets the explorer draw the agent's memory next to the data it reasoned about.

```cypher
MATCH (i:Investigation)-[r:INVOLVES|TARGETS]->(e) WHERE e.id IN $ids
RETURN i.id AS id, i.target_id AS target_id, i.risk_score AS risk_score, i.band AS band,
       type(r) AS rel, e.id AS entity_id
```

"""The ONLY Cypher the application can execute.

Every query is registered here with a name, a purpose and the reason it matters to the agent.
`GraphStore.run(name, params)` refuses unknown names, so neither the API nor an LLM can inject
free-form Cypher. Values are always passed as parameters ($name). The only text substitution is
the variable-length bound `__DEPTH__`, which must be a validated integer (FalkorDB does not
accept parameters for path-length bounds).

`scripts/gen_cypher_docs.py` renders this registry into CYPHER_QUERIES.md, so the document can
never drift away from the code.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    name: str
    group: str
    purpose: str
    why: str
    cypher: str
    write: bool = False
    literals: tuple[str, ...] = ()


_Q: list[Query] = []


def _add(name, group, purpose, why, cypher, write=False, literals=()):
    _Q.append(Query(name, group, purpose, why, cypher.strip(), write, tuple(literals)))


# ---------------------------------------------------------------- 1. entity creation / update
_add("upsert_customers", "Entity creation and updates",
     "Create or update Customer nodes in batch (idempotent MERGE on id).",
     "Every investigation starts from a Customer node; MERGE keeps scenario loading and ingestion repeatable.",
     """
UNWIND $rows AS r
MERGE (c:Customer {id: r.id})
SET c.name = r.name, c.segment = r.segment, c.created_at = r.created_at,
    c.region = r.region, c.source = r.source, c.seq = r.seq
""", write=True)

_add("upsert_devices", "Entity creation and updates",
     "Create or update Device nodes (kind decides whether sharing is meaningful).",
     "Device kind (phone/laptop/tablet/smart_tv/kiosk) lets the agent discount public terminals.",
     """
UNWIND $rows AS r
MERGE (d:Device {id: r.id})
ON CREATE SET d.kind = r.kind, d.label = r.label, d.source = r.source
""", write=True)

_add("upsert_ips", "Entity creation and updates",
     "Create or update IP nodes with a network_type (residential, mobile_carrier, public_wifi, corporate_nat).",
     "Public and carrier-grade NAT addresses are shared by unrelated people; the type is what lets the agent avoid false alarms.",
     """
UNWIND $rows AS r
MERGE (i:IP {id: r.id})
ON CREATE SET i.label = r.label, i.network_type = r.network_type, i.source = r.source
""", write=True)

_add("upsert_products", "Entity creation and updates",
     "Create or update Product nodes.",
     "Products connect orders to categories, which is what the coupon-cluster evidence compares.",
     """
UNWIND $rows AS r
MERGE (p:Product {id: r.id})
ON CREATE SET p.name = r.name, p.category = r.category, p.price = r.price, p.source = r.source
""", write=True)

_add("upsert_sessions", "Entity creation and updates",
     "Create Session nodes and connect them to Customer, Device and IP (provenance stored on each edge).",
     "Sessions are the bridge between customers and shared infrastructure: Customer-Session-Device-Session-Customer is the core multi-hop path.",
     """
UNWIND $rows AS r
MATCH (c:Customer {id: r.customer_id})
MATCH (d:Device {id: r.device_id})
MATCH (ip:IP {id: r.ip_id})
MERGE (s:Session {id: r.id})
ON CREATE SET s.started_at = r.started_at, s.customer_id = r.customer_id, s.source = r.source, s.seq = r.seq
MERGE (c)-[h:HAS_SESSION]->(s) ON CREATE SET h.source = r.source
MERGE (s)-[ud:USED_DEVICE]->(d) ON CREATE SET ud.source = r.source
MERGE (s)-[fi:FROM_IP]->(ip) ON CREATE SET fi.source = r.source
""", write=True)

_add("upsert_events", "Entity creation and updates",
     "Create Event nodes (view / add_to_cart / purchase) and attach them to their Session.",
     "Events are the raw behavioural facts from which entropy and drift are computed.",
     """
UNWIND $rows AS r
MATCH (s:Session {id: r.session_id})
MERGE (e:Event {id: r.id})
SET e.type = r.type, e.ts = r.ts, e.category = r.category, e.product_id = r.product_id,
    e.customer_id = r.customer_id, e.session_id = r.session_id, e.amount = r.amount,
    e.seq = r.seq, e.source = r.source
MERGE (s)-[:HAS_EVENT]->(e)
""", write=True)

_add("upsert_orders", "Entity creation and updates",
     "Create Order nodes linked to Customer, Session and Product.",
     "Orders carry the coupon and product evidence used by the coupon-cluster check.",
     """
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
""", write=True)

_add("link_order_coupons", "Entity creation and updates",
     "Create Coupon nodes and connect the orders that redeemed them.",
     "A coupon redeemed by several linked accounts on the same product category is a strong (but not conclusive) coordination signal.",
     """
UNWIND $rows AS r
MATCH (o:Order {id: r.order_id})
MERGE (k:Coupon {id: r.coupon})
SET k.source = r.source
MERGE (o)-[:USED_COUPON]->(k)
""", write=True)

_add("session_owner", "Customer session retrieval",
     "Return the customer that owns a session id (if it exists).",
     "Ingestion refuses to attach an event to a session that belongs to a different customer.",
     "MATCH (s:Session {id: $id}) RETURN s.customer_id AS customer_id")

# ---------------------------------------------------------------- 2. meta / clock / sequence
_add("meta_set_clock", "Meta and sequencing",
     "Store the scenario clock ('now') and scenario name.",
     "Recent-versus-baseline windows are measured against this clock so results are deterministic.",
     """
MERGE (m:Meta {id: 'clock'})
SET m.now_ts = $now_ts, m.scenario = $scenario
""", write=True)

_add("meta_get_clock", "Meta and sequencing",
     "Read the scenario clock.", "Every behavioural window depends on it.",
     "MATCH (m:Meta {id: 'clock'}) RETURN m.now_ts AS now_ts, m.scenario AS scenario")

_add("meta_next_seq", "Meta and sequencing",
     "Atomically increment and return a monotonic sequence number.",
     "Sequence numbers order ingested events and investigations without relying on wall-clock time, so 'what changed since last time' is exact.",
     """
MERGE (m:Meta {id: 'counter'})
ON CREATE SET m.seq = 0
SET m.seq = m.seq + 1
RETURN m.seq AS seq
""", write=True)

_add("meta_next_investigation_number", "Meta and sequencing",
     "Atomically increment and return the investigation counter.",
     "Gives investigations short, readable, restart-safe ids (INV-00001, INV-00002, ...).",
     """
MERGE (m:Meta {id: 'investigation_counter'})
ON CREATE SET m.n = 0
SET m.n = m.n + 1
RETURN m.n AS n
""", write=True)

_add("meta_current_seq", "Meta and sequencing",
     "Read the current sequence number without changing it.", "Used to stamp an investigation with the graph version it saw.",
     "MATCH (m:Meta {id: 'counter'}) RETURN m.seq AS seq")

# ---------------------------------------------------------------- 3. customer session retrieval
_add("customer_get", "Customer context retrieval",
     "Fetch one customer's profile.", "Confirms the target exists and supplies account age for tenure and proximity checks.",
     """
MATCH (c:Customer {id: $cid})
RETURN c.id AS id, c.name AS name, c.segment AS segment, c.created_at AS created_at, c.region AS region, c.source AS source
""")

_add("customers_by_ids", "Customer context retrieval",
     "Fetch profiles for a list of customers.", "Used to judge linked accounts (age, segment) in one round trip.",
     """
MATCH (c:Customer) WHERE c.id IN $ids
RETURN c.id AS id, c.name AS name, c.segment AS segment, c.created_at AS created_at, c.source AS source
""")

_add("customers_list", "Customer context retrieval",
     "List all customers.", "Feeds the customer picker and the overview.",
     """
MATCH (c:Customer)
RETURN c.id AS id, c.name AS name, c.segment AS segment, c.created_at AS created_at
ORDER BY c.id
""")

_add("customer_sessions", "Customer session retrieval",
     "Retrieve a customer's sessions with the device and IP used in each.",
     "Shows which devices/IPs the customer used and when; new-device detection and the first hop of every path start here.",
     """
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(s:Session)
OPTIONAL MATCH (s)-[:USED_DEVICE]->(d:Device)
OPTIONAL MATCH (s)-[:FROM_IP]->(ip:IP)
RETURN s.id AS session_id, s.started_at AS started_at, d.id AS device_id, d.kind AS device_kind,
       ip.id AS ip_id, ip.network_type AS network_type
ORDER BY s.started_at
""")

_add("sessions_for_customers", "Customer session retrieval",
     "Same as customer_sessions but for many customers at once.",
     "Needed to compute new-device behaviour of linked accounts without one query per account.",
     """
MATCH (c:Customer)-[:HAS_SESSION]->(s:Session) WHERE c.id IN $ids
OPTIONAL MATCH (s)-[:USED_DEVICE]->(d:Device)
OPTIONAL MATCH (s)-[:FROM_IP]->(ip:IP)
RETURN c.id AS customer_id, s.id AS session_id, s.started_at AS started_at, d.id AS device_id,
       d.kind AS device_kind, ip.id AS ip_id, ip.network_type AS network_type
ORDER BY s.started_at
""")

_add("customer_events", "Event timeline retrieval",
     "Retrieve all events of one customer in time order.",
     "The raw material for entropy and drift; also the isolated-event baseline's only data source.",
     """
MATCH (e:Event {customer_id: $cid})
RETURN e.id AS id, e.type AS type, e.ts AS ts, e.category AS category, e.product_id AS product_id,
       e.amount AS amount, e.session_id AS session_id, e.seq AS seq, e.source AS source
ORDER BY e.ts, e.id
""")

_add("events_for_customers", "Event timeline retrieval",
     "Retrieve events for many customers (linked accounts, merged timeline).",
     "Lets the timeline view and the evidence collector see what linked accounts did and when.",
     """
MATCH (e:Event) WHERE e.customer_id IN $ids
RETURN e.id AS id, e.customer_id AS customer_id, e.type AS type, e.ts AS ts, e.category AS category,
       e.product_id AS product_id, e.amount AS amount, e.session_id AS session_id, e.seq AS seq, e.source AS source
ORDER BY e.ts, e.id
""")

_add("events_since", "Event timeline retrieval",
     "Events with sequence number above a threshold for a set of customers.",
     "Identifies exactly which new events arrived since the previous investigation, so the report can explain what changed.",
     """
MATCH (e:Event) WHERE e.customer_id IN $ids AND e.seq > $seq
RETURN e.id AS id, e.customer_id AS customer_id, e.type AS type, e.ts AS ts, e.category AS category,
       e.session_id AS session_id, e.seq AS seq, e.source AS source
ORDER BY e.seq
""")

_add("orders_for_customers", "Customer context retrieval",
     "Orders with product category and coupon, for one or many customers.",
     "Provides order history (tenure evidence) and coupon redemptions (coordination evidence).",
     """
MATCH (c:Customer)-[:PLACED]->(o:Order) WHERE c.id IN $ids
OPTIONAL MATCH (o)-[:CONTAINS]->(p:Product)
OPTIONAL MATCH (o)-[:USED_COUPON]->(k:Coupon)
RETURN c.id AS customer_id, o.id AS order_id, o.placed_at AS placed_at, o.amount AS amount,
       p.id AS product_id, p.category AS category, k.id AS coupon
ORDER BY o.placed_at
""")

# ---------------------------------------------------------------- 4. multi-hop traversal
_add("customer_hop", "Multi-hop relationship traversal",
     "ONE customer-to-customer hop: from a set of customers, find every other customer that shares a device or IP, "
     "returning one representative session pair per (customer, other customer, shared node). The agent calls this once per hop.",
     "This is the core graph step. Aggregating per shared node keeps it fast (no session-pair explosion) while still returning the "
     "exact path Customer-Session-Device/IP-Session-Customer, with device kind / network type so public infrastructure can be discounted. "
     "Calling it again with the newly reached customers gives 2-hop and 3-hop paths.",
     """
MATCH (c:Customer)-[:HAS_SESSION]->(s:Session)-[:USED_DEVICE|FROM_IP]->(x)<-[:USED_DEVICE|FROM_IP]-(s2:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE c.id IN $frontier AND o.id <> c.id
RETURN c.id AS src, o.id AS dst, x.id AS infra_id, labels(x)[0] AS infra_label, x.kind AS kind, x.network_type AS network_type,
       min(s.id) AS src_session, min(s2.id) AS dst_session, count(DISTINCT s) AS src_sessions, count(DISTINCT s2) AS dst_sessions
ORDER BY src, dst, infra_id
""")

_add("customer_paths", "Multi-hop relationship traversal",
     "Variable-length alternative: every raw path from a customer to OTHER customers up to __DEPTH__ relationships (4 = one hop). "
     "Used for the explorer's 'raw paths' view and as an independent cross-check of the hop-by-hop traversal in the tests.",
     "Shows the same multi-hop reachability with a single Cypher pattern. It enumerates every session combination, so it is slower on busy "
     "hubs; the agent therefore uses customer_hop for evidence.",
     """
MATCH p = (c:Customer {id: $cid})-[:HAS_SESSION|USED_DEVICE|FROM_IP*2..__DEPTH__]-(o:Customer)
WHERE o.id <> $cid
RETURN o.id AS other_id, length(p) AS rel_count,
       [n IN nodes(p) | {id: n.id, label: labels(n)[0], kind: n.kind, network_type: n.network_type}] AS nodes,
       [r IN relationships(p) | type(r)] AS rel_types
ORDER BY rel_count
LIMIT $limit
""", literals=("DEPTH",))

_add("coupon_overlap", "Multi-hop relationship traversal",
     "Group orders by (coupon, product category) among a set of customers.",
     "Finds accounts that redeemed the same coupon on the same category; combined with a shared-infrastructure path it becomes "
     "coordination evidence, on its own it is just a popular promotion.",
     """
MATCH (c:Customer)-[:PLACED]->(o:Order)-[:USED_COUPON]->(k:Coupon)
WHERE c.id IN $ids
MATCH (o)-[:CONTAINS]->(p:Product)
RETURN k.id AS coupon, p.category AS category, c.id AS customer_id, o.id AS order_id, o.placed_at AS placed_at
ORDER BY placed_at
""")

# ---------------------------------------------------------------- 5. shared device / shared IP
_add("shared_devices", "Shared-device and shared-IP investigation",
     "Devices used by the customer that are also used by other customers, with those customers and their session counts.",
     "Answers 'who else touches my device?'. Device kind (kiosk vs phone) decides whether sharing is meaningful.",
     """
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(s:Session)-[:USED_DEVICE]->(d:Device)
MATCH (d)<-[:USED_DEVICE]-(s2:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE o.id <> $cid
RETURN d.id AS device_id, d.kind AS kind, o.id AS other_id,
       count(DISTINCT s2) AS other_sessions, min(s2.started_at) AS first_seen, max(s2.started_at) AS last_seen
ORDER BY device_id, other_id
""")

_add("shared_ips", "Shared-device and shared-IP investigation",
     "IP addresses used by the customer that are also used by other customers.",
     "A shared IP is weak evidence: the network_type (public_wifi, corporate_nat, mobile_carrier, residential) tells the agent how much it can mean.",
     """
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(s:Session)-[:FROM_IP]->(ip:IP)
MATCH (ip)<-[:FROM_IP]-(s2:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE o.id <> $cid
RETURN ip.id AS ip_id, ip.network_type AS network_type, o.id AS other_id,
       count(DISTINCT s2) AS other_sessions, min(s2.started_at) AS first_seen, max(s2.started_at) AS last_seen
ORDER BY ip_id, other_id
""")

_add("infra_hub_size", "Shared-device and shared-IP investigation",
     "How many distinct customers use each device/IP the target used.",
     "Large hubs (cafe Wi-Fi, carrier NAT) are explained by ordinary life, not coordination.",
     """
MATCH (c:Customer {id: $cid})-[:HAS_SESSION]->(:Session)-[:USED_DEVICE|FROM_IP]->(x)
MATCH (x)<-[:USED_DEVICE|FROM_IP]-(:Session)<-[:HAS_SESSION]-(o:Customer)
RETURN x.id AS infra_id, labels(x)[0] AS infra_label, count(DISTINCT o) AS customers
ORDER BY customers DESC
""")

# ---------------------------------------------------------------- 6. baseline comparison
_add("scope_comparison", "Baseline comparison",
     "What an isolated-event view sees versus what one graph hop sees: counts of shared infrastructure nodes and reachable customers.",
     "Quantifies, per case, how much extra context the graph provides compared with looking at one customer's own events.",
     """
MATCH (c:Customer {id: $cid})
OPTIONAL MATCH (c)-[:HAS_SESSION]->(:Session)-[:USED_DEVICE|FROM_IP]->(x)<-[:USED_DEVICE|FROM_IP]-(:Session)<-[:HAS_SESSION]-(o:Customer)
WHERE o.id <> $cid
RETURN c.id AS customer_id, count(DISTINCT x) AS shared_infra_nodes, count(DISTINCT o) AS reachable_customers_1hop
""")

# ---------------------------------------------------------------- 7. investigation memory
_add("memory_create_investigation", "Evidence persistence (investigation memory)",
     "Persist an investigation as an Investigation node attached to its target Customer.",
     "This is the agent's long-term memory: it survives backend and database restarts and is the starting point of the next investigation.",
     """
MATCH (c:Customer {id: $target_id})
CREATE (i:Investigation {id: $id, seq: $seq, target_id: $target_id, created_at: $created_at,
                         risk_score: $risk_score, band: $band, flagged: $flagged, conclusion: $conclusion,
                         config_version: $config_version, interpretation_mode: $mode,
                         event_seq: $event_seq, report_json: $report_json, source: 'agent'})
CREATE (i)-[:TARGETS]->(c)
""", write=True)

_add("memory_add_findings", "Evidence persistence (investigation memory)",
     "Persist each piece of evidence as a Finding node linked to the investigation.",
     "Findings are queryable individually (what supported or contradicted a past conclusion?) instead of being buried in text.",
     """
MATCH (i:Investigation {id: $id})
UNWIND $rows AS r
CREATE (f:Finding {id: r.id, kind: r.kind, layer: r.layer, key: r.key, text: r.text, provenance: r.provenance})
CREATE (i)-[:HAS_FINDING]->(f)
""", write=True)

_add("memory_supersede", "Evidence persistence (investigation memory)",
     "Link a new investigation to the previous one for the same target.",
     "Keeps an auditable chain of how a conclusion evolved.",
     """
MATCH (n:Investigation {id: $new_id}), (o:Investigation {id: $old_id})
CREATE (n)-[:SUPERSEDES]->(o)
""", write=True)

for _label, _plural in (("Customer", "customers"), ("Device", "devices"), ("IP", "ips"), ("Coupon", "coupons")):
    _add(f"memory_link_{_plural}", "Evidence persistence (investigation memory)",
         f"Link an investigation to every {_label} node it involved.",
         "These INVOLVES edges are what later lets a different investigation discover this one through shared entities.",
         f"""
MATCH (i:Investigation {{id: $id}})
UNWIND $ids AS x
MATCH (e:{_label} {{id: x}})
MERGE (i)-[:INVOLVES]->(e)
""".replace("{{", "{").replace("}}", "}"), write=True)

_add("memory_by_target", "Previous investigation retrieval",
     "Fetch earlier investigations of the same target, newest first.",
     "Lets the agent compare today's result with its own earlier conclusion (and detect what changed).",
     """
MATCH (i:Investigation {target_id: $cid})
RETURN i.id AS id, i.seq AS seq, i.created_at AS created_at, i.risk_score AS risk_score, i.band AS band,
       i.flagged AS flagged, i.conclusion AS conclusion, i.event_seq AS event_seq, i.report_json AS report_json
ORDER BY i.seq DESC
LIMIT $limit
""")

_add("memory_by_entities", "Previous investigation retrieval",
     "Fetch investigations about OTHER targets that involved any of the given entities.",
     "Graph-native memory: a past finding about a linked account or a shared device is retrieved through the graph, not by keyword.",
     """
MATCH (i:Investigation)-[:INVOLVES]->(e)
WHERE e.id IN $ids AND i.target_id <> $cid
RETURN i.id AS id, i.target_id AS target_id, i.seq AS seq, i.risk_score AS risk_score, i.band AS band,
       i.conclusion AS conclusion, collect(DISTINCT e.id) AS shared_entities
ORDER BY seq DESC
LIMIT $limit
""")

_add("memory_get", "Previous investigation retrieval",
     "Fetch one investigation by id with its full stored report.", "Used by the memory screen and the API.",
     """
MATCH (i:Investigation {id: $id})
RETURN i.id AS id, i.seq AS seq, i.target_id AS target_id, i.created_at AS created_at, i.risk_score AS risk_score,
       i.band AS band, i.conclusion AS conclusion, i.event_seq AS event_seq, i.report_json AS report_json
""")

_add("memory_list", "Previous investigation retrieval",
     "List all stored investigations (without the large JSON blob).", "Powers the Persistent Memory screen.",
     """
MATCH (i:Investigation)
RETURN i.id AS id, i.seq AS seq, i.target_id AS target_id, i.created_at AS created_at, i.risk_score AS risk_score,
       i.band AS band, i.conclusion AS conclusion, i.event_seq AS event_seq
ORDER BY i.seq DESC
LIMIT $limit
""")

_add("memory_findings", "Previous investigation retrieval",
     "List the Finding nodes of one investigation.", "Shows persisted evidence items and their provenance.",
     """
MATCH (i:Investigation {id: $id})-[:HAS_FINDING]->(f:Finding)
RETURN f.id AS id, f.kind AS kind, f.layer AS layer, f.key AS key, f.text AS text, f.provenance AS provenance
ORDER BY f.id
""")

_add("memory_chain", "Previous investigation retrieval",
     "Walk the SUPERSEDES chain from an investigation back in time.", "Audit trail of how conclusions changed.",
     """
MATCH (i:Investigation {id: $id})-[:SUPERSEDES*1..20]->(o:Investigation)
RETURN o.id AS id, o.risk_score AS risk_score, o.band AS band, o.seq AS seq
ORDER BY seq DESC
""")

# ---------------------------------------------------------------- 8. explorer / overview
_add("graph_label_counts", "Overview",
     "Count nodes per label.", "Proves to the user (and judges) that the displayed numbers come from FalkorDB.",
     "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC")

_add("graph_rel_counts", "Overview",
     "Count relationships per type.", "Same purpose as graph_label_counts for edges.",
     "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count ORDER BY count DESC")

_add("explorer_memory", "Graph explorer",
     "Investigations (memory nodes) that involve or target any of the displayed entities.",
     "Lets the explorer draw the agent's memory next to the data it reasoned about.",
     """
MATCH (i:Investigation)-[r:INVOLVES|TARGETS]->(e) WHERE e.id IN $ids
RETURN i.id AS id, i.target_id AS target_id, i.risk_score AS risk_score, i.band AS band,
       type(r) AS rel, e.id AS entity_id
""")

QUERIES: dict[str, Query] = {q.name: q for q in _Q}

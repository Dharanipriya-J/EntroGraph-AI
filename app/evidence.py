"""Turns raw graph rows into path classifications, graph findings and human-readable evidence."""
from __future__ import annotations

from collections import defaultdict

from .config import RiskConfig
from .metrics import BehaviorMetrics, compute_behavior_metrics
from .models import CustomerContext, EvidenceBundle, EvidenceItem, MemoryResult, TraversalResult
from .risk import Counterpart, GraphFindings, TargetFacts
from .store import GraphStore

PRIVATE_KINDS = ("phone", "laptop", "tablet", "smart_tv")
PUBLIC_NETS = ("public_wifi", "corporate_nat")
STRENGTH = {"public_shared": 0, "ip_weak": 1, "private_device": 2}


def segment_strength(kind: str | None, label: str, network_type: str | None) -> int:
    """2 = private device, 1 = non-public IP (residential / mobile carrier), 0 = public or shared-terminal infrastructure."""
    if label == "Device":
        return 2 if kind in PRIVATE_KINDS else 0
    return 0 if network_type in PUBLIC_NETS else 1


CLASS_OF = {2: "private_device", 1: "ip_weak", 0: "public_shared"}


def display_path(nodes: list[dict], rel_types: list[str]) -> str:
    out = [nodes[0]["id"]]
    for i, rel in enumerate(rel_types):
        a = nodes[i]
        forward = (a["label"] == "Customer") if rel == "HAS_SESSION" else (a["label"] == "Session")
        out.append("→" if forward else "←")
        out.append(nodes[i + 1]["id"])
    return " ".join(out)


def _segment_nodes(r: dict) -> tuple[list[dict], list[str]]:
    infra = {k: v for k, v in (("id", r["infra_id"]), ("label", r["infra_label"]), ("kind", r["kind"]), ("network_type", r["network_type"])) if v is not None}
    rel = "USED_DEVICE" if r["infra_label"] == "Device" else "FROM_IP"
    nodes = [{"id": r["src_session"], "label": "Session"}, infra, {"id": r["dst_session"], "label": "Session"}, {"id": r["dst"], "label": "Customer"}]
    return nodes, ["HAS_SESSION", rel, rel, "HAS_SESSION"]


def bfs_paths(run_hop, start_id: str, max_hops: int) -> tuple[dict[str, dict], int]:
    """Hop-by-hop traversal. `run_hop(frontier_ids)` returns customer_hop rows.

    Per reachable customer we keep the best path ranked by (weakest-segment strength, fewer hops), so a ring member reachable over a
    private device is not hidden behind a shorter public-Wi-Fi path. Cycles through the same customer are never followed.
    """
    best: dict[str, dict] = {start_id: {"nodes": [{"id": start_id, "label": "Customer"}], "rel_types": [], "strength": 2, "hops": 0}}
    frontier, total_rows = [start_id], 0
    for hop in range(1, max_hops + 1):
        if not frontier:
            break
        rows = run_hop(frontier)
        total_rows += len(rows)
        improved: list[str] = []
        for r in rows:
            src_path = best[r["src"]]
            if r["dst"] in {n["id"] for n in src_path["nodes"] if n["label"] == "Customer"}:
                continue
            seg_nodes, seg_rels = _segment_nodes(r)
            strength = min(src_path["strength"], segment_strength(r["kind"], r["infra_label"], r["network_type"]))
            cand = {"nodes": src_path["nodes"] + seg_nodes, "rel_types": src_path["rel_types"] + seg_rels, "strength": strength, "hops": hop}
            cur = best.get(r["dst"])
            if cur is None or (cand["strength"], -cand["hops"]) > (cur["strength"], -cur["hops"]):
                best[r["dst"]] = cand
                improved.append(r["dst"])
        frontier = sorted(set(improved))
    out = {}
    for cid, p in best.items():
        if cid == start_id:
            continue
        out[cid] = {"other_id": cid, "rel_count": len(p["rel_types"]), "customer_hops": p["hops"], "nodes": p["nodes"],
                    "rel_types": p["rel_types"], "link_class": CLASS_OF[p["strength"]], "display": display_path(p["nodes"], p["rel_types"]), "alt_paths": 0}
    return out, total_rows


def _tenure(created_at: int, ref_ts: int) -> float:
    return max(0.0, (ref_ts - int(created_at)) / 86400)


def build_findings(store: GraphStore, cfg: RiskConfig, ctx: CustomerContext, traversal: TraversalResult,
                   memory: MemoryResult, target_metrics: BehaviorMetrics) -> tuple[GraphFindings, dict[str, BehaviorMetrics]]:
    """Look up counterpart data (graph queries) and derive the graph-side findings."""
    cid, ref = ctx.customer["id"], ctx.ref_ts
    f = GraphFindings(traversal_enabled=traversal.enabled, max_customer_hops=traversal.max_customer_hops)
    cp_metrics: dict[str, BehaviorMetrics] = {}
    paths = {p["other_id"]: p for p in traversal.paths}
    other_ids = list(paths)
    if other_ids:
        profiles = {r["id"]: r for r in store.run("customers_by_ids", {"ids": other_ids})}
        events = store.run("events_for_customers", {"ids": other_ids})
        sessions = store.run("sessions_for_customers", {"ids": other_ids})
        orders = store.run("orders_for_customers", {"ids": other_ids})
        by = lambda rows: {o: [r for r in rows if r.get("customer_id") == o] for o in other_ids}
        ev_by, se_by, or_by = by(events), by(sessions), by(orders)
        for oid in other_ids:
            p = paths[oid]
            prof = profiles.get(oid, {"name": oid, "created_at": ref})
            sess = [{"device_id": s["device_id"], "started_at": s["started_at"]} for s in se_by[oid]]
            m = compute_behavior_metrics(ev_by[oid], sess, or_by[oid], ref, cfg)
            cp_metrics[oid] = m
            tenure = _tenure(prof["created_at"], ref)
            cutoff = ref - cfg.recent_days * 86400
            prior_orders = sum(1 for o in or_by[oid] if int(o["placed_at"]) <= cutoff)
            established = (tenure >= cfg.established_tenure_days and prior_orders >= cfg.established_min_orders
                           and (m.drift_composite is None or m.drift_composite <= cfg.established_max_drift)
                           and m.recent_coupon_orders == 0)
            f.counterparts.append(Counterpart(
                customer_id=oid, name=prof["name"], customer_hops=p["customer_hops"], rel_count=p["rel_count"],
                link_class=p["link_class"], path_nodes=p["nodes"], rel_types=p["rel_types"], path_display=p["display"],
                established=established, tenure_days=round(tenure, 1), drift=m.drift_composite,
                created_at=int(prof["created_at"]), n_events=len(ev_by[oid]), coupon_recent=m.recent_coupon_orders > 0,
                alt_paths=p.get("alt_paths", 0)))
    # public hubs, kiosks and public-only paths are recorded as ignored links (contradicting evidence)
    for row in traversal.shared_devices:
        if row["kind"] not in PRIVATE_KINDS:
            f.ignored_links.append({"infra_id": row["device_id"], "reason": f"device kind '{row['kind']}' is a public/shared terminal",
                                    "other_id": row["other_id"]})
    for row in traversal.shared_ips:
        if row["network_type"] in PUBLIC_NETS:
            f.ignored_links.append({"infra_id": row["ip_id"], "reason": f"IP network type '{row['network_type']}' is shared by unrelated people",
                                    "other_id": row["other_id"]})
    for c in f.counterparts:
        if c.link_class == "public_shared":
            f.ignored_links.append({"infra_id": c.path_display, "reason": "path runs only through public/shared infrastructure", "other_id": c.customer_id})
    # coupon cluster among target + linked (non-public) accounts, same coupon AND category within the window
    linked_ids = [c.customer_id for c in f.counterparts if c.link_class != "public_shared" and not c.established]
    if linked_ids and cfg.max_customer_hops >= 0:
        rows = store.run("coupon_overlap", {"ids": [cid] + linked_ids})
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            groups[(r["coupon"], r["category"])].append(r)
        best_key, best_members, best_span = None, [], None
        for key, rs in groups.items():
            rs.sort(key=lambda r: r["placed_at"])
            for i in range(len(rs)):  # sliding window
                win = [r for r in rs if 0 <= r["placed_at"] - rs[i]["placed_at"] <= cfg.coupon_window_days * 86400]
                members = sorted({r["customer_id"] for r in win})
                if cid in members and len(members) > len(best_members):
                    best_key, best_members = key, members
                    best_span = (max(r["placed_at"] for r in win) - min(r["placed_at"] for r in win)) / 86400
        f.coupon_cluster_size, f.coupon_cluster_key = len(best_members), best_key
        f.coupon_cluster_members, f.coupon_cluster_span_days = best_members, best_span
    # account age proximity
    created = int(ctx.customer["created_at"])
    f.age_proximity_ids = sorted(c.customer_id for c in f.counterparts if c.link_class != "public_shared" and not c.established
                                 and abs(c.created_at - created) <= cfg.age_proximity_hours * 3600)
    # memory escalation: stored investigations of linked accounts that were rated high
    linked_all = {c.customer_id for c in f.counterparts if c.link_class != "public_shared"}
    f.memory_high_ids = sorted({m["target_id"] for m in memory.related if m["band"] == "high" and m["target_id"] in linked_all})
    return f, cp_metrics


def target_facts(ctx: CustomerContext, cfg: RiskConfig) -> TargetFacts:
    cutoff = ctx.ref_ts - cfg.recent_days * 86400
    return TargetFacts(ctx.customer["id"], _tenure(ctx.customer["created_at"], ctx.ref_ts),
                       sum(1 for o in ctx.orders if int(o["placed_at"]) <= cutoff))


def build_evidence(ctx: CustomerContext, metrics: BehaviorMetrics, findings: GraphFindings, traversal: TraversalResult,
                   memory: MemoryResult, cfg: RiskConfig, facts: TargetFacts) -> EvidenceBundle:
    cid = ctx.customer["id"]
    sup: list[EvidenceItem] = []
    con: list[EvidenceItem] = []
    mis: list[EvidenceItem] = []
    prov = lambda q, **kw: {"queries": q if isinstance(q, list) else [q], **kw}

    # ---- behavioural (computed metrics)
    if metrics.drift_composite is not None:
        txt = (f"Category drift {metrics.drift_category:.3f} bits, time-of-day drift {metrics.drift_hour:.3f} bits "
               f"(composite {metrics.drift_composite:.3f}); normalised entropy {metrics.category_baseline.normalized_entropy:.2f} → "
               f"{metrics.category_recent.normalized_entropy:.2f}.")
        if metrics.drift_composite >= 0.25:
            sup.append(EvidenceItem("drift", "supporting", "computed_metric", "Recent behaviour differs from the customer's baseline. " + txt,
                                    [cid], prov(["customer_events"])))
        else:
            con.append(EvidenceItem("drift_low", "contradicting", "computed_metric", "Behaviour is consistent with the customer's own baseline. " + txt,
                                    [cid], prov(["customer_events"])))
    else:
        mis.append(EvidenceItem("baseline_insufficient", "missing", "computed_metric",
                                "Not enough history to compute behavioural drift: " + "; ".join(metrics.notes), [cid], prov(["customer_events"])))
    if metrics.new_devices_recent:
        sup.append(EvidenceItem("new_device", "supporting", "observed_fact",
                                f"Device(s) used only recently: {metrics.new_devices_recent}.", metrics.new_devices_recent, prov(["customer_sessions"])))
    if metrics.recent_coupon_orders:
        sup.append(EvidenceItem("recent_coupon", "supporting", "observed_fact",
                                f"{metrics.recent_coupon_orders} recent order(s) used a coupon.", [cid], prov(["orders_for_customers"])))

    # ---- graph evidence
    for c in findings.counterparts:
        src = {"queries": ["customer_paths"], "path": c.path_display}
        if c.link_class == "public_shared":
            continue
        if c.established:
            con.append(EvidenceItem(f"established_link:{c.customer_id}", "contradicting", "observed_fact",
                                    f"Linked account {c.customer_id} ({c.path_display}) is long-standing ({c.tenure_days:.0f} days) and behaviourally stable — "
                                    "consistent with ordinary household or family sharing, not coordination.", [c.customer_id], src))
        elif c.link_class == "private_device":
            sup.append(EvidenceItem(f"linked:private_device:{c.customer_id}", "supporting", "observed_fact",
                                    f"{c.customer_id} is connected through a private device, {c.customer_hops} customer hop(s) away: {c.path_display}. "
                                    f"Account age {c.tenure_days:.0f} days, {c.n_events} events.", [c.customer_id], src))
        else:
            sup.append(EvidenceItem(f"linked:ip_weak:{c.customer_id}", "supporting", "observed_fact",
                                    f"{c.customer_id} shares a residential/mobile IP ({c.path_display}). Weak evidence: shared IPs are common.",
                                    [c.customer_id], src))
        if c.n_events == 0:
            mis.append(EvidenceItem(f"no_activity:{c.customer_id}", "missing", "observed_fact",
                                    f"Linked account {c.customer_id} has no recorded events, so its behaviour cannot be assessed.", [c.customer_id],
                                    prov(["events_for_customers"])))
    if findings.coupon_cluster_size >= cfg.coupon_cluster_min_customers and findings.coupon_cluster_key:
        k = findings.coupon_cluster_key
        sup.append(EvidenceItem(f"coupon_cluster:{k[0]}:{k[1]}", "supporting", "observed_fact",
                                f"{findings.coupon_cluster_size} linked accounts {findings.coupon_cluster_members} redeemed coupon '{k[0]}' on category "
                                f"'{k[1]}' within {findings.coupon_cluster_span_days:.1f} days.", findings.coupon_cluster_members,
                                prov(["coupon_overlap"])))
    elif any(c.link_class != "public_shared" and not c.established for c in findings.counterparts):
        con.append(EvidenceItem("no_coupon_cluster", "contradicting", "observed_fact",
                                "Linked accounts do not share a coupon-and-category redemption with the target.", [cid], prov(["coupon_overlap"])))
    if findings.age_proximity_ids:
        sup.append(EvidenceItem("age_proximity", "supporting", "observed_fact",
                                f"Linked accounts {findings.age_proximity_ids} were created within {cfg.age_proximity_hours}h of the target.",
                                findings.age_proximity_ids, prov(["customers_by_ids"])))
    if findings.memory_high_ids:
        sup.append(EvidenceItem("memory_escalation", "supporting", "observed_fact",
                                f"Stored earlier investigations rated linked accounts {findings.memory_high_ids} as high risk.",
                                findings.memory_high_ids, prov(["memory_by_entities"])))
    for ig in findings.ignored_links:
        con.append(EvidenceItem(f"ignored_link:{ig['infra_id']}:{ig['other_id']}", "contradicting", "observed_fact",
                                f"Link to {ig['other_id']} via {ig['infra_id']} not counted: {ig['reason']}.", [ig["other_id"]],
                                prov(["shared_devices", "shared_ips"])))
    big = [h for h in traversal.hubs if h["customers"] >= 4 and h["infra_label"] == "IP"]
    for h in big:
        con.append(EvidenceItem(f"hub:{h['infra_id']}", "contradicting", "observed_fact",
                                f"{h['infra_id']} is used by {h['customers']} different customers — a busy shared resource, not a private link.",
                                [h["infra_id"]], prov(["infra_hub_size"])))
    # ---- mitigations / benign context
    if cfg.use_mitigations and facts.tenure_days >= cfg.established_tenure_days and facts.prior_orders >= cfg.established_min_orders:
        con.append(EvidenceItem("established_tenure", "contradicting", "observed_fact",
                                f"The target account is {facts.tenure_days:.0f} days old with {facts.prior_orders} earlier orders.", [cid],
                                prov(["customer_get", "orders_for_customers"])))
    if not findings.counterparts and traversal.enabled:
        con.append(EvidenceItem("no_links", "contradicting", "observed_fact",
                                f"No other customer is reachable within {findings.max_customer_hops} customer hop(s) through sessions, devices or IPs.",
                                [cid], prov(["customer_paths"])))
    if not traversal.enabled:
        mis.append(EvidenceItem("traversal_disabled", "missing", "observed_fact",
                                "Graph traversal was disabled for this run, so linked accounts could not be examined.", [cid], {}))
    # ---- always-missing data (model limitations)
    mis.append(EvidenceItem("no_payment_or_address", "missing", "observed_fact",
                            "The graph has no payment-instrument or shipping-address entities, so shared payment methods or delivery addresses "
                            "(strong coordination evidence in practice) cannot be checked.", [], {}))
    mis.append(EvidenceItem("no_outcome_data", "missing", "observed_fact",
                            "No chargeback, refund, login-failure or customer-confirmation data is available.", [], {}))
    if traversal.enabled and findings.counterparts and not memory.related and not memory.same_target:
        mis.append(EvidenceItem("no_prior_memory", "missing", "observed_fact",
                                "No earlier investigation of this customer or its linked accounts exists in memory.", [cid], prov(["memory_by_target", "memory_by_entities"])))
    # ---- benign explanations (only for signals that actually fired)
    ben: list[str] = []
    if metrics.drift_composite is not None and metrics.drift_composite >= 0.25:
        ben.append("Behaviour change can reflect a new hobby, a gift purchase, a seasonal sale or a life event.")
    if metrics.new_devices_recent:
        ben.append("A new device is often a replaced or additional phone or laptop.")
    if metrics.recent_coupon_orders or findings.coupon_cluster_size:
        ben.append("Coupons are normally public promotions; many unrelated customers redeem the same one.")
    if any(c.link_class == "private_device" for c in findings.counterparts):
        ben.append("A shared device can mean family or household members using one phone, tablet or laptop.")
    if any(c.link_class == "ip_weak" for c in findings.counterparts) or traversal.shared_ips:
        ben.append("Shared IP addresses occur on mobile carrier NAT, home routers, offices and cafés.")
    if findings.age_proximity_ids:
        ben.append("Accounts created close together can belong to people who signed up for the same promotion or campaign.")
    if not ben:
        ben.append("No suspicious signal fired; ordinary customer behaviour is the most likely explanation.")
    return EvidenceBundle(sup, con, mis, ben, findings)

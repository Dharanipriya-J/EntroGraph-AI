"""Typed, testable agent tools. Each tool does one thing and talks to FalkorDB only through registered queries."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any, Callable

from .config import RiskConfig
from .evidence import bfs_paths, build_evidence, build_findings, target_facts
from .metrics import BehaviorMetrics, compute_behavior_metrics
from .models import (CustomerContext, CustomerInput, EventIn, EvidenceBundle, MemoryInput, MemoryResult,
                     TraversalResult, TraverseInput)
from .store import GraphStore


class ToolError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AgentTools:
    def __init__(self, store: GraphStore, cfg: RiskConfig | None = None):
        self.store = store
        self.cfg = cfg or RiskConfig()

    # 1 ------------------------------------------------------------------ customer context
    def get_customer_context(self, customer_id: str) -> CustomerContext:
        rows = self.store.run("customer_get", {"cid": customer_id})
        if not rows:
            raise ToolError(f"customer {customer_id!r} not found", 404)
        clock = self.store.clock()
        events = self.store.run("customer_events", {"cid": customer_id})
        ref = clock["now_ts"] or (max((int(e["ts"]) for e in events), default=int(time.time())))
        return CustomerContext(
            customer=rows[0], sessions=self.store.run("customer_sessions", {"cid": customer_id}), events=events,
            orders=self.store.run("orders_for_customers", {"ids": [customer_id]}), ref_ts=int(ref), scenario=clock["scenario"])

    # 2 ------------------------------------------------------------------ behavioural metrics
    def calculate_behavior_metrics(self, ctx: CustomerContext) -> BehaviorMetrics:
        return compute_behavior_metrics(ctx.events, ctx.sessions, ctx.orders, ctx.ref_ts, self.cfg)

    # 3 ------------------------------------------------------------------ multi-hop traversal
    def traverse_relationships(self, customer_id: str, max_customer_hops: int | None = None) -> TraversalResult:
        hops = self.cfg.max_customer_hops if max_customer_hops is None else max_customer_hops
        TraverseInput(customer_id=customer_id, max_customer_hops=hops)  # validate
        if hops == 0:
            return TraversalResult(enabled=False, max_customer_hops=0)
        best, total = bfs_paths(lambda ids: self.store.run("customer_hop", {"frontier": ids}), customer_id, hops)
        return TraversalResult(
            enabled=True, max_customer_hops=hops, paths=sorted(best.values(), key=lambda p: (p["customer_hops"], p["other_id"])),
            all_path_count=total, shared_devices=self.store.run("shared_devices", {"cid": customer_id}),
            shared_ips=self.store.run("shared_ips", {"cid": customer_id}), hubs=self.store.run("infra_hub_size", {"cid": customer_id}))

    # 4 ------------------------------------------------------------------ memory retrieval
    def get_investigation_memory(self, customer_id: str, entity_ids: list[str] | None = None, limit: int = 10) -> MemoryResult:
        MemoryInput(customer_id=customer_id, entity_ids=entity_ids or [], limit=limit)
        same = self.store.run("memory_by_target", {"cid": customer_id, "limit": limit})
        for r in same:
            r["report"] = json.loads(r.pop("report_json")) if r.get("report_json") else None
        related = self.store.run("memory_by_entities", {"cid": customer_id, "ids": entity_ids, "limit": limit}) if entity_ids else []
        return MemoryResult(same_target=same, related=related)

    # 5 ------------------------------------------------------------------ evidence
    def collect_evidence(self, ctx: CustomerContext, metrics: BehaviorMetrics, traversal: TraversalResult,
                         memory: MemoryResult) -> EvidenceBundle:
        findings, cp_metrics = build_findings(self.store, self.cfg, ctx, traversal, memory, metrics)
        facts = target_facts(ctx, self.cfg)
        bundle = build_evidence(ctx, metrics, findings, traversal, memory, self.cfg, facts)
        bundle.cp_metrics = cp_metrics  # type: ignore[attr-defined]
        bundle.facts = facts  # type: ignore[attr-defined]
        return bundle

    # 6 ------------------------------------------------------------------ save
    def save_investigation(self, report: dict, involved: dict[str, list[str]]) -> str:
        """Persist the report as an Investigation node, Finding nodes and INVOLVES/TARGETS/SUPERSEDES edges."""
        seq = self.store.next_seq()
        inv_id = f"INV-{int(self.store.run('meta_next_investigation_number')[0]['n']):05d}"
        report["investigation_id"] = inv_id
        report["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        risk = report["risk"]
        self.store.run("memory_create_investigation", {
            "id": inv_id, "seq": seq, "target_id": report["target"]["id"], "created_at": report["created_at"],
            "risk_score": risk["score"], "band": risk["band"], "flagged": risk["flagged"],
            "conclusion": report["conclusion"], "config_version": risk["config_version"],
            "mode": report["layers"]["ai_interpretation"]["mode"], "event_seq": report["graph_seq"],
            "report_json": json.dumps(report, default=str)})
        rows, n = [], 0
        for kind in ("supporting", "contradicting", "missing"):
            for e in report["evidence"][kind]:
                n += 1
                rows.append({"id": f"{inv_id}-F{n:02d}", "kind": kind, "layer": e["layer"], "key": e["key"], "text": e["text"],
                             "provenance": json.dumps(e["provenance"], default=str)})
        if rows:
            self.store.run("memory_add_findings", {"id": inv_id, "rows": rows})
        for plural, ids in involved.items():
            if ids:
                self.store.run(f"memory_link_{plural}", {"id": inv_id, "ids": sorted(set(ids))})
        prev = report.get("changes") or {}
        if prev.get("previous_investigation_id"):
            self.store.run("memory_supersede", {"new_id": inv_id, "old_id": prev["previous_investigation_id"]})
        return inv_id

    # 7 ------------------------------------------------------------------ ingest
    def ingest_event(self, ev: EventIn) -> dict:
        """Validate and write one event (plus its session, device, IP, order and coupon) to the graph."""
        if not self.store.run("customer_get", {"cid": ev.customer_id}):
            raise ToolError(f"customer {ev.customer_id!r} not found", 404)
        if ev.type == "purchase" and ev.amount is None:
            raise ToolError("purchase events require 'amount'", 422)
        seq = self.store.next_seq()
        ts = ev.ts if ev.ts is not None else int(self.store.clock()["now_ts"] or time.time())
        session_id = ev.session_id or f"S-LIVE-{seq}"
        owner = self.store.run("session_owner", {"id": session_id})
        if owner and owner[0]["customer_id"] != ev.customer_id:
            raise ToolError(f"session {session_id!r} belongs to another customer", 409)
        src = "api:ingest"
        product_id = f"P-{ev.category}"
        self.store.run("upsert_devices", {"rows": [{"id": ev.device_id, "kind": ev.device_kind, "label": f"{ev.device_kind} {ev.device_id}", "source": src}]})
        self.store.run("upsert_ips", {"rows": [{"id": ev.ip_id, "label": f"{ev.network_type} {ev.ip_id}", "network_type": ev.network_type, "source": src}]})
        self.store.run("upsert_products", {"rows": [{"id": product_id, "name": f"{ev.category} item", "category": ev.category, "price": ev.amount or 0, "source": src}]})
        self.store.run("upsert_sessions", {"rows": [{"id": session_id, "customer_id": ev.customer_id, "device_id": ev.device_id, "ip_id": ev.ip_id,
                                                     "started_at": ts, "source": src, "seq": seq}]})
        event_id = f"E-LIVE-{seq}"
        self.store.run("upsert_events", {"rows": [{"id": event_id, "session_id": session_id, "customer_id": ev.customer_id, "type": ev.type, "ts": ts,
                                                   "category": ev.category, "product_id": product_id, "amount": ev.amount, "source": src, "seq": seq}]})
        order_id = None
        if ev.type == "purchase":
            order_id = ev.order_id or f"O-LIVE-{seq}"
            self.store.run("upsert_orders", {"rows": [{"id": order_id, "customer_id": ev.customer_id, "session_id": session_id, "product_id": product_id,
                                                       "placed_at": ts, "amount": ev.amount, "source": src, "seq": seq}]})
            if ev.coupon:
                self.store.run("link_order_coupons", {"rows": [{"order_id": order_id, "coupon": ev.coupon, "source": src}]})
        return {"seq": seq, "event_id": event_id, "session_id": session_id, "order_id": order_id, "ts": ts, "customer_id": ev.customer_id}

    # 8 ------------------------------------------------------------------ baseline comparison
    def compare_with_baseline(self, customer_id: str) -> dict:
        from .baseline import compare_methods
        return compare_methods(self.store, customer_id, self.cfg)


# --------------------------------------------------------------------------- registry
def tool_specs() -> list[dict]:
    """Machine-readable list of tools (name, description, JSON schema of inputs) for GET /api/tools."""
    return [
        {"name": "get_customer_context", "description": "Retrieve a customer's profile, sessions, events and orders from FalkorDB.",
         "input_schema": CustomerInput.model_json_schema(), "graph_queries": ["customer_get", "customer_events", "customer_sessions", "orders_for_customers", "meta_get_clock"]},
        {"name": "calculate_behavior_metrics", "description": "Entropy, normalised entropy and Jensen-Shannon drift (baseline vs recent) from the retrieved events.",
         "input_schema": CustomerInput.model_json_schema(), "graph_queries": []},
        {"name": "traverse_relationships", "description": "Multi-hop traversal (customer-session-device/IP-session-customer) plus shared-device/IP and hub checks.",
         "input_schema": TraverseInput.model_json_schema(), "graph_queries": ["customer_hop", "shared_devices", "shared_ips", "infra_hub_size"]},
        {"name": "get_investigation_memory", "description": "Retrieve previous investigations of the same target and of related entities.",
         "input_schema": MemoryInput.model_json_schema(), "graph_queries": ["memory_by_target", "memory_by_entities"]},
        {"name": "collect_evidence", "description": "Build supporting, contradicting and missing evidence from context, metrics, paths and memory.",
         "input_schema": CustomerInput.model_json_schema(), "graph_queries": ["customers_by_ids", "events_for_customers", "sessions_for_customers", "orders_for_customers", "coupon_overlap"]},
        {"name": "save_investigation", "description": "Persist the report, findings and links as Investigation memory in FalkorDB.",
         "input_schema": CustomerInput.model_json_schema(), "graph_queries": ["memory_create_investigation", "memory_add_findings", "memory_link_customers", "memory_link_devices", "memory_link_ips", "memory_link_coupons", "memory_supersede"]},
        {"name": "ingest_event", "description": "Validate and ingest a synthetic event, creating/linking session, device, IP, order and coupon.",
         "input_schema": EventIn.model_json_schema(), "graph_queries": ["meta_next_seq", "session_owner", "upsert_devices", "upsert_ips", "upsert_products", "upsert_sessions", "upsert_events", "upsert_orders", "link_order_coupons"]},
        {"name": "compare_with_baseline", "description": "Run the isolated-event baseline, a naive shared-infrastructure rule and the graph-assisted method on the same customer.",
         "input_schema": CustomerInput.model_json_schema(), "graph_queries": ["scope_comparison", "shared_devices", "shared_ips"]},
    ]

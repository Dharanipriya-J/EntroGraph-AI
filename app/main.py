"""FastAPI application: JSON API + static investigation console."""
from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import InvestigationAgent
from .config import Settings, get_risk_config, get_settings
from .evaluation import run_evaluation
from .evidence import bfs_paths
from .interpreter import make_interpreter
from .models import EventIn, InvestigateRequest, ResetRequest
from .scenarios import SCENARIO_ID, cases, demo_event, load_scenario
from .store import GraphStore, QueryError
from .tools import AgentTools, ToolError, tool_specs

STATIC = Path(__file__).parent / "static"
ROOT = Path(__file__).resolve().parent.parent


def create_app(settings: Settings | None = None, store: GraphStore | None = None) -> FastAPI:
    settings = settings or get_settings()
    cfg = get_risk_config()
    write_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store or GraphStore(settings)
        if settings.auto_load_scenario and not app.state.store.run("customers_list"):
            load_scenario(app.state.store)
        app.state.last_eval = None
        app.state.board_cache = {}
        yield
        if store is None:
            app.state.store.close()

    app = FastAPI(title="EntroGraph AI", version="1.0.0", lifespan=lifespan,
                  description="Graph-native investigation agent on FalkorDB (synthetic data only).")
    if store is not None:  # tests pass a ready store; lifespan may not run with TestClient without a context manager
        app.state.store = store
        app.state.last_eval = None
        app.state.board_cache = {}

    def agent() -> InvestigationAgent:
        return InvestigationAgent(app.state.store, cfg, make_interpreter(settings))

    @app.exception_handler(ToolError)
    async def _tool_error(_: Request, exc: ToolError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.exception_handler(QueryError)
    async def _query_error(_: Request, exc: QueryError):
        return JSONResponse({"error": "graph query failed", "detail": str(exc)}, status_code=503)

    # ------------------------------------------------------------ health / overview / config
    @app.get("/api/health")
    def health():
        st: GraphStore = app.state.store
        return {"status": "ok" if st.ping() else "database_unreachable", "db_mode": settings.db_mode, "graph": st.graph_name,
                "interpretation_mode": make_interpreter(settings).__class__.__name__, "llm_configured": bool(settings.anthropic_api_key),
                "scenario": st.clock().get("scenario")}

    @app.get("/api/config")
    def get_config():
        return {"risk_config": cfg.to_dict(), "version": cfg.version(), "behaviour_max_points": cfg.behaviour_max}

    @app.get("/api/tools")
    def tools():
        return {"tools": tool_specs()}

    @app.get("/api/overview")
    def overview():
        st: GraphStore = app.state.store
        seq = st.current_seq()
        cache = app.state.board_cache
        if cache.get("seq") != seq:
            board = []
            ag = agent()
            for c in st.run("customers_list"):
                r = ag.investigate(c["id"], persist=False)
                board.append({"customer_id": c["id"], "name": c["name"], "score": r["risk"]["score"], "band": r["risk"]["band"],
                              "linked": [p["other_id"] for p in r["graph_paths"] if p["link_class"] != "public_shared"]})
            board.sort(key=lambda b: -b["score"])
            cache.clear(); cache.update({"seq": seq, "board": board})
        mem = st.run("memory_list", {"limit": 8})
        return {"clock": st.clock(), "labels": st.run("graph_label_counts"), "relationships": st.run("graph_rel_counts"),
                "leaderboard": cache["board"][:10], "recent_investigations": mem,
                "note": "Leaderboard = graph-assisted risk assessments computed live from FalkorDB (not saved to memory)."}

    # ------------------------------------------------------------ customers
    @app.get("/api/customers")
    def customers():
        rows = app.state.store.run("customers_list")
        truth = {c.customer_id: {"label": c.label, "group": c.group} for c in cases()}
        for r in rows:
            r["synthetic_truth"] = truth.get(r["id"])
        return {"customers": rows}

    @app.get("/api/customers/{customer_id}/context")
    def context(customer_id: str):
        from .models import CustomerInput
        CustomerInput(customer_id=customer_id)
        ctx = AgentTools(app.state.store, cfg).get_customer_context(customer_id)
        return {"customer": ctx.customer, "sessions": ctx.sessions, "orders": ctx.orders, "n_events": len(ctx.events)}

    @app.get("/api/customers/{customer_id}/timeline")
    def timeline(customer_id: str, include_linked: bool = True, hops: int = Query(2, ge=0, le=3)):
        st: GraphStore = app.state.store
        tools_ = AgentTools(st, cfg)
        tools_.get_customer_context(customer_id)  # 404 if missing
        ids = [customer_id]
        if include_linked and hops > 0:
            best, _ = bfs_paths(lambda f: st.run("customer_hop", {"frontier": f}), customer_id, hops)
            ids += [k for k, v in best.items() if v["link_class"] != "public_shared"]
        sess = {s["session_id"]: s for s in st.run("sessions_for_customers", {"ids": ids})}
        items = []
        for e in st.run("events_for_customers", {"ids": ids}):
            s = sess.get(e["session_id"], {})
            items.append({**e, "device_id": s.get("device_id"), "ip_id": s.get("ip_id"), "is_target": e["customer_id"] == customer_id})
        orders = {o["order_id"]: o for o in st.run("orders_for_customers", {"ids": ids})}
        coupons = [{"order_id": k, "customer_id": v["customer_id"], "coupon": v["coupon"], "placed_at": v["placed_at"]} for k, v in orders.items() if v["coupon"]]
        memory = st.run("memory_by_target", {"cid": customer_id, "limit": 20})
        for m in memory:
            m.pop("report_json", None)
        return {"customer_id": customer_id, "linked_customers": ids[1:], "events": items, "coupon_redemptions": coupons, "investigations": memory}

    # ------------------------------------------------------------ investigation
    @app.post("/api/investigate")
    def investigate(req: InvestigateRequest):
        with write_lock:
            return agent().investigate(req.customer_id, persist=req.persist, max_customer_hops=req.max_customer_hops)

    @app.get("/api/compare/{customer_id}")
    def compare(customer_id: str):
        from .models import CustomerInput
        CustomerInput(customer_id=customer_id)
        return AgentTools(app.state.store, cfg).compare_with_baseline(customer_id)

    # ------------------------------------------------------------ memory
    @app.get("/api/investigations")
    def investigations(customer_id: str | None = None, limit: int = Query(50, ge=1, le=200)):
        st = app.state.store
        if customer_id:
            rows = st.run("memory_by_target", {"cid": customer_id, "limit": limit})
            for r in rows:
                r.pop("report_json", None)
            return {"investigations": rows}
        return {"investigations": st.run("memory_list", {"limit": limit})}

    @app.get("/api/investigations/{inv_id}")
    def investigation(inv_id: str):
        st = app.state.store
        rows = st.run("memory_get", {"id": inv_id})
        if not rows:
            raise HTTPException(404, f"investigation {inv_id!r} not found")
        row = rows[0]
        row["report"] = json.loads(row.pop("report_json")) if row.get("report_json") else None
        row["findings"] = st.run("memory_findings", {"id": inv_id})
        row["supersedes"] = st.run("memory_chain", {"id": inv_id})
        return row

    # ------------------------------------------------------------ graph explorer
    @app.get("/api/graph/neighborhood")
    def neighborhood(customer_id: str, hops: int = Query(2, ge=1, le=3), sessions: bool = True, commerce: bool = True, memory: bool = True):
        st: GraphStore = app.state.store
        AgentTools(st, cfg).get_customer_context(customer_id)
        best, _ = bfs_paths(lambda f: st.run("customer_hop", {"frontier": f}), customer_id, hops)
        nodes: dict[str, dict] = {}
        edges: dict[tuple, dict] = {}

        def add(n: dict):
            key = f"{n['label']}:{n['id']}"
            nodes.setdefault(key, {"id": key, "raw_id": n["id"], "label": n["label"], "kind": n.get("kind"), "network_type": n.get("network_type")})
            return key

        add({"id": customer_id, "label": "Customer"})
        on_path = set()
        for oid, p in best.items():
            ns, rels = p["nodes"], p["rel_types"]
            for i, rel in enumerate(rels):
                a, b = ns[i], ns[i + 1]
                if not sessions and "Session" in (a["label"], b["label"]):
                    continue
                forward = (a["label"] == "Customer") if rel == "HAS_SESSION" else (a["label"] == "Session")
                src, dst = (add(a), add(b)) if forward else (add(b), add(a))
                edges[(src, dst, rel)] = {"source": src, "target": dst, "type": rel, "link_class": p["link_class"]}
                on_path.update([src, dst])
            if not sessions:  # collapse Customer-Session-Infra into Customer-Infra
                for i, n in enumerate(ns):
                    if n["label"] in ("Device", "IP"):
                        for j in (i - 2, i + 2):
                            if 0 <= j < len(ns) and ns[j]["label"] == "Customer":
                                c = add(ns[j]); x = add(n)
                                edges[(c, x, "USES")] = {"source": c, "target": x, "type": "USES", "link_class": p["link_class"]}
        cust_ids = sorted({n["raw_id"] for n in nodes.values() if n["label"] == "Customer"})
        if commerce:
            for o in st.run("orders_for_customers", {"ids": cust_ids}):
                if o["coupon"]:
                    c, k = add({"id": o["customer_id"], "label": "Customer"}), add({"id": o["coupon"], "label": "Coupon"})
                    edges[(c, k, "REDEEMED")] = {"source": c, "target": k, "type": "REDEEMED", "category": o["category"]}
        if memory:
            ids = [n["raw_id"] for n in nodes.values()]
            for m in st.run("explorer_memory", {"ids": ids}):
                i = add({"id": m["id"], "label": "Investigation"})
                nodes[i].update(risk_score=m["risk_score"], band=m["band"], target_id=m["target_id"])
                for lab in ("Customer", "Device", "IP", "Coupon"):
                    t = f"{lab}:{m['entity_id']}"
                    if t in nodes:
                        edges[(i, t, m["rel"])] = {"source": i, "target": t, "type": m["rel"]}
        return {"center": f"Customer:{customer_id}", "nodes": list(nodes.values()), "edges": list(edges.values()),
                "queries": ["customer_hop", "orders_for_customers", "explorer_memory"], "paths": [
                    {"other_id": k, "display": v["display"], "link_class": v["link_class"], "customer_hops": v["customer_hops"]} for k, v in best.items()]}

    @app.get("/api/graph/raw-paths")
    def raw_paths(customer_id: str, customer_hops: int = Query(1, ge=1, le=2), limit: int = Query(50, ge=1, le=300)):
        rows = app.state.store.run("customer_paths", {"cid": customer_id, "limit": limit}, {"DEPTH": 4 * customer_hops})
        return {"paths": rows, "note": "Variable-length Cypher view: every raw session-level path (slower on busy hubs)."}

    # ------------------------------------------------------------ ingestion / reset / restart
    @app.post("/api/events")
    def ingest(ev: EventIn):
        with write_lock:
            res = AgentTools(app.state.store, cfg).ingest_event(ev)
        return {**res, "graph_seq": app.state.store.current_seq()}

    @app.post("/api/scenario/reset")
    def reset(req: ResetRequest | None = None):
        with write_lock:
            summary = load_scenario(app.state.store, reset=True)
            app.state.board_cache.clear()
        return summary

    @app.post("/api/admin/restart")
    def restart():
        """Simulate a backend restart: drop the database connection and every in-process cache, reconnect, re-read memory.

        (A real process/container restart is covered in DEMO_SCRIPT.md and the restart test.)"""
        with write_lock:
            st: GraphStore = app.state.store
            st.close()
            if st._owns_client is False:
                raise HTTPException(409, "cannot restart a shared-connection store")
            st.query_log.clear()
            st.connect()
            app.state.board_cache.clear()
            mem = st.run("memory_list", {"limit": 200})
        return {"restarted": True, "memory_investigations_found": len(mem), "latest": mem[:3]}

    # ------------------------------------------------------------ challenge
    @app.get("/api/challenge/info")
    def challenge_info():
        return {"scenario": SCENARIO_ID, "target": "A1", "new_event": demo_event(),
                "story": ("Anika (A1) looks only mildly unusual. The graph reveals that her phone sessions share device DEV-X with Bala (B1), "
                          "both redeemed NEWUSER50 on gift cards, and both accounts were created within a day. A long-standing household "
                          "member (C1) also uses DEV-X and is correctly treated as benign context. Then dormant account D1 appears on DEV-X.")}

    @app.post("/api/challenge/new-event")
    def challenge_event():
        with write_lock:
            st = app.state.store
            if any(e["source"] == "api:ingest" for e in st.run("customer_events", {"cid": "D1"})):
                raise HTTPException(409, "the challenge event was already applied; reset the scenario to replay it")
            res = AgentTools(st, cfg).ingest_event(EventIn(**demo_event()))
        return {**res, "event": demo_event()}

    # ------------------------------------------------------------ evaluation
    @app.post("/api/evaluation/run")
    def evaluation_run():
        with write_lock:
            app.state.last_eval = run_evaluation(app.state.store, cfg)
        return app.state.last_eval

    @app.get("/api/evaluation/latest")
    def evaluation_latest():
        if app.state.last_eval:
            return app.state.last_eval
        f = ROOT / "docs" / "evaluation_results.json"
        if f.exists():
            return {**json.loads(f.read_text()), "source": "docs/evaluation_results.json (precomputed)"}
        raise HTTPException(404, "no evaluation yet; POST /api/evaluation/run")

    # ------------------------------------------------------------ static
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app() if os.environ.get("ENTROGRAPH_NO_AUTOAPP") != "1" else None

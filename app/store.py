"""FalkorDB access layer. All queries go through `GraphStore.run`, which only accepts registered query names."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from .config import Settings, get_settings
from .queries import QUERIES

# (label, property) pairs that get a range index. Unique constraints are intentionally not used:
# FalkorDB requires a supporting index first and has had constraint-related crash reports, so
# idempotency is enforced with MERGE on indexed ids instead (see GRAPH_SCHEMA.md).
INDEXES = [
    ("Customer", "id"), ("Session", "id"), ("Device", "id"), ("IP", "id"), ("Product", "id"),
    ("Order", "id"), ("Coupon", "id"), ("Event", "id"), ("Event", "customer_id"), ("Meta", "id"),
    ("Investigation", "id"), ("Investigation", "target_id"), ("Finding", "id"),
]


class QueryError(RuntimeError):
    pass


class GraphStore:
    def __init__(self, settings: Settings | None = None, graph_name: str | None = None, client: Any = None):
        self.settings = settings or get_settings()
        self.graph_name = graph_name or self.settings.graph_name
        self._client: Any = client
        self._owns_client = client is None
        self._graph: Any = None
        self._lock = threading.RLock()
        self.query_log: deque[dict] = deque(maxlen=2000)
        self._counter = 0
        if client is None:
            self.connect()
        else:  # share an existing connection (used for the isolated evaluation graph)
            self._graph = client.select_graph(self.graph_name)
            self.ensure_schema()

    def sibling(self, graph_name: str) -> "GraphStore":
        """A second graph in the same FalkorDB instance (shares the connection)."""
        return GraphStore(self.settings, graph_name=graph_name, client=self._client)

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        s = self.settings
        if s.db_mode == "embedded":
            from redislite import FalkorDB  # provided by the optional `falkordblite` package

            self._client = FalkorDB(s.embedded_path)
        elif s.db_mode == "server":
            from falkordb import FalkorDB

            last: Exception | None = None
            for _ in range(30):  # the database container may still be starting (docker compose)
                try:
                    client = FalkorDB(host=s.db_host, port=s.db_port, password=s.db_password)
                    client.execute_command("PING")
                    self._client = client
                    break
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    time.sleep(1)
            else:
                raise ConnectionError(f"cannot reach FalkorDB at {s.db_host}:{s.db_port}: {last}")
        else:
            raise ValueError(f"unknown FALKORDB_MODE {s.db_mode!r} (use 'server' or 'embedded')")
        self._graph = self._client.select_graph(self.graph_name)
        self.ensure_schema()

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    if self._owns_client:
                        self._client.close()
                finally:
                    self._client, self._graph = None, None

    def ping(self) -> bool:
        try:
            self._client.execute_command("PING")
            return True
        except Exception:
            return False

    def ensure_schema(self) -> None:
        for label, prop in INDEXES:
            try:
                self._graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as exc:  # already indexed -> fine
                if "already indexed" not in str(exc).lower() and "already exists" not in str(exc).lower():
                    raise

    def reset(self) -> None:
        """Delete the whole graph (GRAPH.DELETE) and recreate the indexes."""
        with self._lock:
            try:
                self._graph.delete()
            except Exception:
                pass  # graph did not exist yet
            self._graph = self._client.select_graph(self.graph_name)
            self.ensure_schema()

    # ------------------------------------------------------------ query execution
    def run(self, name: str, params: dict | None = None, literals: dict[str, int] | None = None) -> list[dict]:
        q = QUERIES.get(name)
        if q is None:
            raise QueryError(f"unknown query {name!r}: only registered queries may be executed")
        text = q.cypher
        literals = literals or {}
        if set(literals) != set(q.literals):
            raise QueryError(f"query {name!r} expects literals {q.literals}, got {tuple(literals)}")
        for key, val in literals.items():
            if not isinstance(val, int) or isinstance(val, bool) or not (0 < val <= 24):
                raise QueryError(f"literal {key} must be an integer in 1..24")
            text = text.replace(f"__{key}__", str(val))
        t0 = time.perf_counter()
        try:
            with self._lock:
                res = self._graph.query(text, params or {})
        except Exception as exc:
            raise QueryError(f"query {name!r} failed: {exc}") from exc
        ms = (time.perf_counter() - t0) * 1000
        header = [h[1] for h in res.header] if res.header else []
        rows = [dict(zip(header, row)) for row in res.result_set] if header else []
        with self._lock:
            self._counter += 1
            self.query_log.append({"n": self._counter, "name": name, "ms": round(ms, 2), "rows": len(rows), "write": q.write})
        return rows

    def log_position(self) -> int:
        return self._counter

    def log_since(self, position: int) -> list[dict]:
        return [e for e in list(self.query_log) if e["n"] > position]

    # ------------------------------------------------------------ small helpers
    def next_seq(self) -> int:
        return int(self.run("meta_next_seq")[0]["seq"])

    def current_seq(self) -> int:
        rows = self.run("meta_current_seq")
        return int(rows[0]["seq"]) if rows else 0

    def clock(self) -> dict:
        rows = self.run("meta_get_clock")
        return rows[0] if rows else {"now_ts": None, "scenario": None}

"""Render CYPHER_QUERIES.md from app/queries.py so the document can never drift from the code.

Usage: python scripts/gen_cypher_docs.py [--check]
"""
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.queries import QUERIES  # noqa: E402

HEADER = """# CYPHER_QUERIES.md

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

"""


def render() -> str:
    groups: "OrderedDict[str, list]" = OrderedDict()
    for q in QUERIES.values():
        groups.setdefault(q.group, []).append(q)
    out = [HEADER]
    for g, qs in groups.items():
        out.append(f"## {g}\n")
        for q in qs:
            out.append(f"### `{q.name}`{' (write)' if q.write else ''}\n")
            out.append(f"**Purpose:** {q.purpose}\n")
            out.append(f"**Why it matters to the agent:** {q.why}\n")
            out.append("```cypher\n" + q.cypher + "\n```\n")
    return "\n".join(out)


if __name__ == "__main__":
    target = ROOT / "CYPHER_QUERIES.md"
    text = render()
    if "--check" in sys.argv:
        sys.exit(0 if target.exists() and target.read_text() == text else 1)
    target.write_text(text)
    print(f"wrote {target} ({len(QUERIES)} queries)")

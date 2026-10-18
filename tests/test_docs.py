import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_cypher_doc_matches_registry():
    r = subprocess.run([sys.executable, "scripts/gen_cypher_docs.py", "--check"], cwd=ROOT)
    assert r.returncode == 0, "CYPHER_QUERIES.md is stale: run python scripts/gen_cypher_docs.py"


def test_required_files_exist():
    for f in ["README.md", "ARCHITECTURE.md", "GRAPH_SCHEMA.md", "CYPHER_QUERIES.md", "EVALUATION.md", "DEMO_SCRIPT.md", ".env.example",
              ".gitignore", "requirements.txt", "Dockerfile", "docker-compose.yml", "LICENSE"]:
        assert (ROOT / f).exists(), f

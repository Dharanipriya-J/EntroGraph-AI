import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.agent import InvestigationAgent
from app.config import Settings
from app.scenarios import load_scenario
from app.store import GraphStore

ROOT = Path(__file__).resolve().parent.parent


def test_memory_survives_closing_and_reopening_the_database(tmp_path):
    s = Settings(db_mode="embedded", embedded_path=str(tmp_path / "p.rdb"), graph_name="persist")
    st = GraphStore(s)
    load_scenario(st)
    rep = InvestigationAgent(st).investigate("A1", persist=True)
    st.close()
    st2 = GraphStore(s)  # fresh connection / engine start from disk
    rows = st2.run("memory_by_target", {"cid": "A1", "limit": 5})
    assert rows and rows[0]["id"] == rep["investigation_id"] and rows[0]["band"] == "medium"
    # a re-investigation in the "new process" sees the old one
    again = InvestigationAgent(st2).investigate("A1", persist=True)
    assert again["changes"]["previous_investigation_id"] == rep["investigation_id"]
    assert again["investigation_id"] != rep["investigation_id"]  # counters survive too
    st2.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(port, db):
    env = {**os.environ, "FALKORDB_MODE": "embedded", "FALKORDB_EMBEDDED_PATH": db, "ENTROGRAPH_NO_AUTOAPP": "0"}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2).status_code == 200:
                return p
        except Exception:
            time.sleep(0.5)
    p.kill()
    raise RuntimeError("server did not start")


def test_real_process_restart_keeps_investigation_memory(tmp_path):
    """Starts the real server, investigates, SIGTERMs the process, starts a NEW process and reads the memory back."""
    db, port = str(tmp_path / "srv.rdb"), _free_port()
    p = _start(port, db)
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/api/investigate", json={"customer_id": "A1"}, timeout=30).json()
        inv = r["investigation_id"]
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
    assert p.poll() is not None
    p2 = _start(port, db)
    try:
        lst = httpx.get(f"http://127.0.0.1:{port}/api/investigations", timeout=30).json()["investigations"]
        assert [i["id"] for i in lst] == [inv]
        again = httpx.post(f"http://127.0.0.1:{port}/api/investigate", json={"customer_id": "A1"}, timeout=30).json()
        assert again["changes"]["previous_investigation_id"] == inv
    finally:
        p2.send_signal(signal.SIGTERM)
        p2.wait(timeout=30)


@pytest.mark.skipif(not os.environ.get("FALKORDB_TEST_HOST"), reason="needs a real FalkorDB server (Docker): set FALKORDB_TEST_HOST")
def test_server_mode_against_docker_falkordb():
    s = Settings(db_mode="server", db_host=os.environ["FALKORDB_TEST_HOST"], db_port=int(os.environ.get("FALKORDB_TEST_PORT", "6379")),
                 db_password=os.environ.get("FALKORDB_PASSWORD"), graph_name="entrograph_ci")
    st = GraphStore(s)
    load_scenario(st)
    rep = InvestigationAgent(st).investigate("A1", persist=True)
    assert rep["risk"]["band"] == "medium" and rep["investigation_id"]
    st.close()

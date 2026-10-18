import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ENTROGRAPH_NO_AUTOAPP", "1")

from app.config import Settings  # noqa: E402
from app.scenarios import load_scenario  # noqa: E402
from app.store import GraphStore  # noqa: E402


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    """Real FalkorDB engine (embedded via falkordblite) in a temp directory; no mocks."""
    d = tmp_path_factory.mktemp("fdb")
    return Settings(db_mode="embedded", embedded_path=str(d / "test.rdb"), graph_name="entrograph_test")


@pytest.fixture(scope="session")
def base_store(settings):
    st = GraphStore(settings)
    yield st
    st.close()


@pytest.fixture
def store(base_store):
    load_scenario(base_store, reset=True)
    return base_store

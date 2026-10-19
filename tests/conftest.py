"""Pytest configuration and shared fixtures.

The suite runs against a temporary copy of the real database and the real
corpus (``./data`` by default, override with ``TEST_DATA_DIR``), with
``APP_TODAY=2026-10-01`` so date rules are reproducible. Environment
variables are set here, before any ``app`` module is imported, because
settings are cached.

The vector index is cached in ``.cache/test_vector_store`` and reused while
the corpus fingerprint is unchanged; the database copy is always fresh, so
holidays are re-extracted into it on start-up.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TEST_DATA_DIR", PROJECT_ROOT / "data")).resolve()
TODAY = "2026-10-01"

_tmp = Path(tempfile.mkdtemp(prefix="veltrix_tests_"))
_db_copy = _tmp / "veltrix.db"
shutil.copy(DATA_DIR / "veltrix.db", _db_copy)

os.environ.update(
    APP_TODAY=TODAY,
    DB_PATH=str(_db_copy),
    CORPUS_DIR=str(DATA_DIR / "corpus"),
    MANIFEST_PATH=str(DATA_DIR / "manifest.json"),
    VECTOR_STORE_PATH=str(PROJECT_ROOT / ".cache" / "test_vector_store"),
    LOG_LEVEL="WARNING",
    # Tests must never call a real LLM; document answers use a fake client.
    OPENAI_API_KEY="",
    ANTHROPIC_API_KEY="",
    GOOGLE_API_KEY="",
    LLM_PROVIDER="openai",
    OPENAI_BASE_URL="",
    OPENAI_EXTRA_BODY="",
)
os.environ.setdefault("HF_HOME", str(PROJECT_ROOT / ".hf_cache"))

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.data_access.database import get_database  # noqa: E402
from app.data_access.employee_repo import EmployeeRepository  # noqa: E402
from app.main import app, state  # noqa: E402


class FakeLLM:
    """Deterministic stand-in for the LLM: records calls, returns canned JSON."""

    def __init__(self):
        self.calls: list[dict] = []
        self.status = "answered"
        self.raw = None  # set to return this exact text instead of JSON

    @property
    def available(self) -> bool:
        return True

    def complete(self, system_prompt, user_prompt, *, purpose, corpus_tokens, **kwargs):
        self.calls.append(
            {"system": system_prompt, "user": user_prompt, "corpus_tokens": corpus_tokens}
        )
        if self.raw is not None:
            return self.raw
        import json

        return json.dumps(
            {
                "status": self.status,
                "answer": "Answer composed from the passages.",
                "used_passages": ["P1"] if self.status in ("answered", "partial") else [],
                "used_upcoming": [],
            }
        )


@pytest.fixture(scope="session")
def client():
    """Started service (ingestion done) shared by the whole session."""
    with TestClient(app) as test_client:
        state.thread.join(timeout=900)
        assert state.ready, f"service failed to start: {state.error}"
        yield test_client


@pytest.fixture(scope="session")
def handler(client):
    return state.chat_handler


@pytest.fixture
def fake_llm(handler):
    """Swap in the fake LLM for one test."""
    original = handler.llm_client
    fake = FakeLLM()
    handler.llm_client = fake
    yield fake
    handler.llm_client = original


@pytest.fixture
def employees(client):
    repo = EmployeeRepository()
    return lambda employee_id: repo.get_by_id(employee_id)


@pytest.fixture
def restore_leave_data(client):
    """Snapshot leave tables and restore them after a test that writes."""
    db = get_database()
    requests = [tuple(r) for r in db.execute("SELECT * FROM leave_requests")]
    balances = [tuple(r) for r in db.execute("SELECT * FROM leave_balances")]
    yield
    with db.transaction() as conn:
        conn.execute("DELETE FROM leave_requests")
        conn.executemany(f"INSERT INTO leave_requests VALUES ({','.join('?' * 8)})", requests)
        conn.execute("DELETE FROM leave_balances")
        conn.executemany("INSERT INTO leave_balances VALUES (?, ?, ?)", balances)


def pytest_sessionfinish(session, exitstatus):
    try:
        get_database().close()
    except Exception:
        pass
    shutil.rmtree(_tmp, ignore_errors=True)

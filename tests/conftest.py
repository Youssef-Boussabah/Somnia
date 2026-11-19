"""Test harness: every test session runs against a fresh, real PostgreSQL + pgvector database.

The database is created from SOMNIA_DATABASE_URL's server (see .env), migrated, and dropped at
the end. There is no fallback or skip: if PostgreSQL is unreachable, the tests fail.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from somnia import budget
from somnia.artifacts import ArtifactStore
from somnia.config import Settings
from somnia.contracts import RunConfig
from somnia.db import connect
from somnia.engine import create_run
from somnia.events import record_fixture_session
from somnia.migrate import migrate
from somnia.projects import create_project
from somnia.snapshots import create_snapshot

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "development"
EXPERIENCE = FIXTURES / "experience_indexer_checkpoint.json"


def _with_db(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture(scope="session")
def db_url() -> str:
    base = Settings.from_env().database_url
    name = f"somnia_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = _with_db(base, name)
    with connect(url) as c:
        assert migrate(c) == [1, 2]
    yield url
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def connect_db(db_url):
    opened: list[psycopg.Connection] = []

    def _open() -> psycopg.Connection:
        c = connect(db_url)
        opened.append(c)
        return c

    yield _open
    for c in opened:
        if not c.closed:
            c.close()


@pytest.fixture
def conn(connect_db):
    return connect_db()


@pytest.fixture
def artifacts(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def project(conn):
    slug = f"t-{uuid.uuid4().hex[:12]}"
    return {"slug": slug, "id": create_project(conn, slug)}


def load_config(**overrides) -> RunConfig:
    data = json.loads((FIXTURES / "run_config.mock.json").read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        node = data
        *path, last = dotted.split("__")
        for p in path:
            node = node[p]
        node[last] = value
    return RunConfig.model_validate(data)


def make_run(conn, project_id, *, script="indexer_checkpoint_v1", envelope_limit=200_000, envelope="mock-dev",
             key=None, session_id=None, config=None, snapshot_id=None):
    """Record the synthetic experience (unless given), snapshot it, and create a pending run."""
    if snapshot_id is None:
        if session_id is None:
            session_id, _ = record_fixture_session(conn, project_id, EXPERIENCE)
        snapshot_id = create_snapshot(conn, project_id, [session_id]).snapshot_id
    env = budget.create_envelope(conn, project_id, envelope, unit="MOCK_MICRO", limit_micro=envelope_limit)
    cfg = config or load_config(mock_script=script)
    rid = create_run(conn, project_id=project_id, snapshot_id=snapshot_id, envelope_id=env, config=cfg,
                     idempotency_key=key or f"run-{uuid.uuid4().hex}")
    return {"run_id": rid, "envelope_id": env, "snapshot_id": snapshot_id, "session_id": session_id}


def wait_lease_expired(conn, run_id, timeout=10.0) -> None:
    """Wait (real time, DB clock) until the run's lease has expired."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = conn.execute("SELECT lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp() AS expired"
                         " FROM sleep_runs WHERE id = %s", (run_id,)).fetchone()
        if r["expired"]:
            return
        time.sleep(0.05)
    raise AssertionError("lease did not expire in time")

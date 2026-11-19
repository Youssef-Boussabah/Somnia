"""Upgrade from a foundation (0001) database to 0002, on real PostgreSQL."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from conftest import _with_db, load_config, make_run
from somnia.artifacts import ArtifactStore
from somnia.config import Settings
from somnia.db import connect
from somnia.engine import SleepWorker
from somnia.errors import ActivationInvariantViolation, translating
from somnia.export_verify import verify_skill_set_export
from somnia.migrate import migrate, status
from somnia.projects import active_skill_set_id, create_project
from somnia.skills import export_skill_set, skill_set_export_digest


@pytest.fixture
def foundation_db():
    """A separate database migrated to 0001 only, as a foundation-era database would be."""
    base = Settings.from_env().database_url
    name = f"somnia_upg_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = _with_db(base, name)
    conns: list[psycopg.Connection] = []

    def _open():
        c = connect(url)
        conns.append(c)
        return c
    c = _open()
    assert migrate(c, target=1) == [1]
    yield _open
    for c in conns:
        c.close()
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_upgrade_keeps_foundation_data_and_enforces_new_invariants(foundation_db, tmp_path):
    conn = foundation_db()
    art = ArtifactStore(tmp_path / "artifacts")
    pid = create_project(conn, "upgrade-a")
    run = make_run(conn, pid)  # data written while only 0001 is installed
    assert SleepWorker(conn, art, worker_id="w").work(run["run_id"]).status == "completed"
    set_v1 = active_skill_set_id(conn, pid)
    digest_before = skill_set_export_digest(export_skill_set(conn, set_v1))

    assert migrate(conn) == [2]
    assert migrate(conn) == []
    assert [(s["version"], s["state"]) for s in status(conn)] == [(1, "ok"), (2, "ok")]

    # Existing records still export identically and verify offline.
    exp = export_skill_set(conn, set_v1)
    assert skill_set_export_digest(exp) == digest_before
    verify_skill_set_export(exp.model_dump_json(), digest_before)
    # A new cycle completes on the upgraded database (its candidate revises the existing skill).
    from somnia.snapshots import create_snapshot
    snap2 = create_snapshot(conn, pid, [run["session_id"]]).snapshot_id
    run2 = make_run(conn, pid, snapshot_id=snap2, envelope="mock-dev-2",
                    config=load_config(mock_script="indexer_checkpoint_revision_v1"))
    assert SleepWorker(conn, art, worker_id="w2").work(run2["run_id"]).status == "completed"
    assert export_skill_set(conn, active_skill_set_id(conn, pid)).version == 2

    # The new triggers reject cross-project membership and pointers, even from hand-written SQL.
    other = create_project(conn, "upgrade-b")
    other_set = active_skill_set_id(conn, other)
    m = conn.execute("SELECT skill_id, skill_version_id, gate_decision_id FROM skill_set_members"
                     " WHERE skill_set_id = %s", (set_v1,)).fetchone()
    with pytest.raises(ActivationInvariantViolation), translating():
        conn.execute("INSERT INTO skill_set_members VALUES (%s, %s, %s, %s)",
                     (other_set, m["skill_id"], m["skill_version_id"], m["gate_decision_id"]))
    with pytest.raises(ActivationInvariantViolation), translating():
        conn.execute("UPDATE active_skill_sets SET skill_set_id = %s WHERE project_id = %s", (set_v1, other))
    assert active_skill_set_id(conn, other) == other_set


def test_upgrade_refuses_foundation_data_that_crosses_projects(foundation_db):
    conn = foundation_db()
    a, b = create_project(conn, "cross-a"), create_project(conn, "cross-b")
    # Hand-made 0001-era corruption: a gate-passed skill of project A listed in project B's set.
    from somnia.contracts import SkillBody
    from somnia.skills import create_skill_version
    with conn.transaction():
        sv = create_skill_version(conn, project_id=a, body=SkillBody(
            slug="x", title="t", procedure="p", applicability={"domain": "d"}, exclusions={}, known_limits="k"),
            parent_version_id=None, origin="user", run_id=None)
    g = conn.execute(
        "INSERT INTO gate_decisions (skill_version_id, candidate_hash, baseline_skill_set_id, validator,"
        " validator_version, suite_id, suite_hash, independent, is_mock, decision, reason, paired_scores,"
        " evidence_refs) VALUES (%s, %s, %s, 'x', '1', 's', 'h', false, true, 'pass', 'r', '[]', '[]') RETURNING id",
        (sv["skill_version_id"], sv["content_hash"], active_skill_set_id(conn, a))).fetchone()["id"]
    conn.execute("INSERT INTO skill_set_members VALUES (%s, %s, %s, %s)",
                 (active_skill_set_id(conn, b), sv["skill_id"], sv["skill_version_id"], g))
    with pytest.raises(ActivationInvariantViolation, match="cross a project boundary"), translating():
        migrate(conn)
    assert [(s["version"], s["state"]) for s in status(conn)] == [(1, "ok"), (2, "pending")]

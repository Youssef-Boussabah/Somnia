"""Event recording, partition isolation of learning snapshots, and migrations."""

from __future__ import annotations

import threading

import psycopg
import pytest

from conftest import EXPERIENCE
from somnia.errors import FixtureError, IdempotencyConflict, PartitionViolation, SequenceError, translating
from somnia.events import load_experience_fixture, open_session, record_event, record_fixture_session
from somnia.migrate import migrate
from somnia.snapshots import create_snapshot


def _session(conn, project, partition="development"):
    return open_session(conn, project["id"], partition=partition, origin="synthetic", purpose="experience",
                        agent_config_hash="h")


def test_event_retry_is_idempotent_and_conflicts_are_rejected(conn, project):
    s = _session(conn, project)
    a = record_event(conn, session_id=s, seq=1, event_type="t", payload={"x": 1}, idempotency_key="e1")
    b = record_event(conn, session_id=s, seq=1, event_type="t", payload={"x": 1}, idempotency_key="e1")
    assert a.event_id == b.event_id and a.created and not b.created
    with pytest.raises(IdempotencyConflict):
        record_event(conn, session_id=s, seq=1, event_type="t", payload={"x": 2}, idempotency_key="e1")
    with pytest.raises(SequenceError):
        record_event(conn, session_id=s, seq=3, event_type="t", payload={}, idempotency_key="e3")


def test_concurrent_appends_produce_a_gapless_chain(connect_db, conn, project):
    s = _session(conn, project)
    barrier = threading.Barrier(8)
    outcomes = []

    def append(i):
        c = connect_db()
        barrier.wait()
        try:
            outcomes.append(record_event(c, session_id=s, seq=1, event_type="t", payload={"i": i},
                                         idempotency_key=f"k{i}"))
        except SequenceError as exc:
            outcomes.append(exc)

    ts = [threading.Thread(target=append, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sum(not isinstance(o, Exception) for o in outcomes) == 1
    assert conn.execute("SELECT count(*) AS n FROM events WHERE session_id = %s", (s,)).fetchone()["n"] == 1


def test_learning_snapshot_only_accepts_development_experience(conn, project):
    v = _session(conn, project, partition="validation")
    ev = record_event(conn, session_id=v, seq=1, event_type="t", payload={}, idempotency_key="v1")
    with pytest.raises(PartitionViolation):
        create_snapshot(conn, project["id"], [v])
    # The database enforces the rule even if application checks are bypassed.
    d, _ = record_fixture_session(conn, project["id"], EXPERIENCE)
    snap = create_snapshot(conn, project["id"], [d])
    with pytest.raises(PartitionViolation), translating(), conn.transaction():
        conn.execute("INSERT INTO snapshot_events (snapshot_id, event_id, event_hash) VALUES (%s, %s, %s)",
                     (snap.snapshot_id, ev.event_id, ev.event_hash))
    # 'final' is not a representable partition in the learner store at all.
    with pytest.raises(psycopg.errors.CheckViolation):
        _session(conn, project, partition="final")


def test_snapshot_is_idempotent_by_content(conn, project):
    d, _ = record_fixture_session(conn, project["id"], EXPERIENCE)
    a = create_snapshot(conn, project["id"], [d])
    b = create_snapshot(conn, project["id"], [d])
    assert a.snapshot_id == b.snapshot_id and a.created and not b.created and a.event_count == 5


def test_unlabelled_fixture_is_refused(tmp_path):
    p = tmp_path / "f.json"
    p.write_text('{"label": "real data", "synthetic": false, "events": []}', encoding="utf-8")
    with pytest.raises(FixtureError):
        load_experience_fixture(p)


def test_migrations_are_idempotent_and_detect_edits(conn):
    assert migrate(conn) == []
    original = conn.execute("SELECT sha256 FROM public.somnia_schema_migrations WHERE version = 1").fetchone()["sha256"]
    conn.execute("UPDATE public.somnia_schema_migrations SET sha256 = 'edited' WHERE version = 1")
    try:
        with pytest.raises(RuntimeError, match="modified"):
            migrate(conn)
    finally:
        conn.execute("UPDATE public.somnia_schema_migrations SET sha256 = %s WHERE version = 1", (original,))
    ext = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()["extversion"]
    assert ext == "0.8.7"

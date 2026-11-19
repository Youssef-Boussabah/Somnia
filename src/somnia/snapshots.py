"""Immutable learning input snapshots: a frozen set of development events plus the base skill set."""

from __future__ import annotations

from uuid import UUID

import psycopg

from .contracts import SnapshotInfo
from .errors import PartitionViolation, translating
from .hashing import hash_obj
from .projects import active_skill_set_id


def create_snapshot(conn: psycopg.Connection, project_id: UUID, session_ids: list[UUID]) -> SnapshotInfo:
    """Freeze the given sessions' events and the currently active skill set. Idempotent by content hash."""
    if not session_ids:
        raise ValueError("a snapshot needs at least one session")
    with translating(), conn.transaction():
        sessions = conn.execute(
            "SELECT id, partition, project_id FROM sessions WHERE id = ANY(%s)", (list(session_ids),)
        ).fetchall()
        if len(sessions) != len(set(session_ids)):
            raise LookupError("unknown session id in snapshot request")
        for s in sessions:
            if s["project_id"] != project_id:
                raise PartitionViolation(f"session {s['id']} belongs to another project")
            if s["partition"] != "development":
                raise PartitionViolation(f"session {s['id']} is {s['partition']!r}; learning uses development only")
        events = conn.execute(
            "SELECT e.id, e.event_hash FROM events e JOIN sessions s ON s.id = e.session_id"
            " WHERE e.session_id = ANY(%s) ORDER BY s.created_at, s.id, e.seq", (list(session_ids),),
        ).fetchall()
        if not events:
            raise ValueError("selected sessions contain no events")
        base = active_skill_set_id(conn, project_id)
        content_hash = hash_obj({"base_skill_set_id": base, "events": [e["event_hash"] for e in events]})
        row = conn.execute(
            "INSERT INTO snapshots (project_id, content_hash, base_skill_set_id, event_count)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (project_id, content_hash) DO NOTHING RETURNING id",
            (project_id, content_hash, base, len(events)),
        ).fetchone()
        created = row is not None
        if created:
            sid = row["id"]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO snapshot_events (snapshot_id, event_id, event_hash) VALUES (%s, %s, %s)",
                    [(sid, e["id"], e["event_hash"]) for e in events],
                )
        else:
            sid = conn.execute(
                "SELECT id FROM snapshots WHERE project_id = %s AND content_hash = %s", (project_id, content_hash)
            ).fetchone()["id"]
    return SnapshotInfo(snapshot_id=sid, project_id=project_id, content_hash=content_hash, base_skill_set_id=base,
                        event_count=len(events), created=created)


def event_ref(session_ordinal: int, idempotency_key: str) -> str:
    """Snapshot-local, unambiguous event reference used in model prompts, e.g. ``s2/evt-002``.

    Idempotency keys are unique only within a session, so a key alone can name several events of a
    multi-session snapshot. ``session_ordinal`` is the 1-based position of the event's session in
    the snapshot's fixed order (session created_at, then id). The prefix up to the first ``/`` is
    ``s`` + digits, so distinct (session, key) pairs always give distinct refs. A ref means nothing
    outside its snapshot; persisted evidence stores the resolved event id, never the ref."""
    return f"s{session_ordinal}/{idempotency_key}"


def snapshot_events(conn: psycopg.Connection, snapshot_id: UUID) -> list[dict]:
    rows = conn.execute(
        "SELECT e.id, e.session_id, e.idempotency_key, e.seq, e.event_type, e.payload, e.event_hash, e.origin,"
        " dense_rank() OVER (ORDER BY s.created_at, s.id) AS session_ordinal"
        " FROM snapshot_events se JOIN events e ON e.id = se.event_id JOIN sessions s ON s.id = e.session_id"
        " WHERE se.snapshot_id = %s ORDER BY s.created_at, s.id, e.seq", (snapshot_id,),
    ).fetchall()
    for r in rows:
        r["ref"] = event_ref(r["session_ordinal"], r["idempotency_key"])
    return rows

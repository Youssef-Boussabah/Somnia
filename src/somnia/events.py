"""Append-only, hash-chained experience events and development-fixture ingestion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .contracts import EventReceipt
from .errors import FixtureError, IdempotencyConflict, SequenceError
from .hashing import hash_obj


def open_session(conn: psycopg.Connection, project_id: UUID, *, partition: str, origin: str, purpose: str,
                 agent_config_hash: str, label: str | None = None) -> UUID:
    row = conn.execute(
        "INSERT INTO sessions (project_id, partition, origin, purpose, agent_config_hash, label)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (project_id, partition, origin, purpose, agent_config_hash, label),
    ).fetchone()
    return row["id"]


def _event_hash(session_id: UUID, seq: int, event_type: str, payload_hash: str, artifact: str | None,
                prev_hash: str | None, origin: str) -> str:
    return hash_obj({"session_id": session_id, "seq": seq, "event_type": event_type, "payload_hash": payload_hash,
                     "artifact_sha256": artifact, "prev_hash": prev_hash, "origin": origin})


def record_event(conn: psycopg.Connection, *, session_id: UUID, seq: int, event_type: str, payload: dict[str, Any],
                 idempotency_key: str, artifact_sha256: str | None = None) -> EventReceipt:
    """Append one event. Sequence numbers must be contiguous per session. Retrying with the same
    idempotency key and identical content returns the existing event; different content is a conflict."""
    payload_hash = hash_obj(payload)
    with conn.transaction():
        sess = conn.execute("SELECT project_id, origin FROM sessions WHERE id = %s FOR UPDATE", (session_id,)).fetchone()
        if sess is None:
            raise LookupError(f"unknown session {session_id}")
        existing = conn.execute(
            "SELECT id, seq, event_type, payload_hash, artifact_sha256, event_hash FROM events"
            " WHERE session_id = %s AND idempotency_key = %s", (session_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            same = (existing["seq"], existing["event_type"], existing["payload_hash"], existing["artifact_sha256"]) == (
                seq, event_type, payload_hash, artifact_sha256)
            if not same:
                raise IdempotencyConflict(f"idempotency key {idempotency_key!r} reused with different content")
            return EventReceipt(event_id=existing["id"], session_id=session_id, seq=seq,
                                event_hash=existing["event_hash"], created=False)
        last = conn.execute(
            "SELECT seq, event_hash FROM events WHERE session_id = %s ORDER BY seq DESC LIMIT 1", (session_id,)
        ).fetchone()
        expected = 1 if last is None else last["seq"] + 1
        if seq != expected:
            raise SequenceError(f"session {session_id}: expected seq {expected}, got {seq}")
        prev_hash = None if last is None else last["event_hash"]
        ev_hash = _event_hash(session_id, seq, event_type, payload_hash, artifact_sha256, prev_hash, sess["origin"])
        row = conn.execute(
            "INSERT INTO events (project_id, session_id, seq, event_type, payload, artifact_sha256, origin,"
            " idempotency_key, payload_hash, prev_hash, event_hash)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (sess["project_id"], session_id, seq, event_type, Jsonb(payload), artifact_sha256, sess["origin"],
             idempotency_key, payload_hash, prev_hash, ev_hash),
        ).fetchone()
    return EventReceipt(event_id=row["id"], session_id=session_id, seq=seq, event_hash=ev_hash, created=True)


def verify_session_chain(conn: psycopg.Connection, session_id: UUID) -> list[str]:
    """Recompute payload and chain hashes. Returns a list of problems (empty when intact)."""
    problems: list[str] = []
    rows = conn.execute(
        "SELECT seq, event_type, payload, artifact_sha256, origin, payload_hash, prev_hash, event_hash"
        " FROM events WHERE session_id = %s ORDER BY seq", (session_id,),
    ).fetchall()
    prev = None
    for r in rows:
        if hash_obj(r["payload"]) != r["payload_hash"]:
            problems.append(f"seq {r['seq']}: payload hash mismatch")
        if r["prev_hash"] != prev:
            problems.append(f"seq {r['seq']}: broken chain link")
        if _event_hash(session_id, r["seq"], r["event_type"], r["payload_hash"], r["artifact_sha256"], r["prev_hash"],
                       r["origin"]) != r["event_hash"]:
            problems.append(f"seq {r['seq']}: event hash mismatch")
        prev = r["event_hash"]
    return problems


def load_experience_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("synthetic") is not True or "SYNTHETIC" not in data.get("label", ""):
        raise FixtureError(f"{path}: experience fixtures must be explicitly labelled synthetic")
    return data


def record_fixture_session(conn: psycopg.Connection, project_id: UUID, path: Path) -> tuple[UUID, list[EventReceipt]]:
    """Record a synthetic development session from a fixture file (origin='synthetic')."""
    data = load_experience_fixture(path)
    session_id = open_session(conn, project_id, partition="development", origin="synthetic", purpose="experience",
                              agent_config_hash=hash_obj(data["agent_config"]), label=data["session_label"])
    receipts = [
        record_event(conn, session_id=session_id, seq=i, event_type=ev["type"], payload=ev["payload"],
                     idempotency_key=ev["key"])
        for i, ev in enumerate(data["events"], start=1)
    ]
    return session_id, receipts

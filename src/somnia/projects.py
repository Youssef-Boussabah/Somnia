"""Projects. Creating a project also creates the empty genesis skill set and active pointer."""

from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .hashing import hash_obj

EMPTY_SET_HASH = hash_obj({"members": []})


def create_project(conn: psycopg.Connection, slug: str) -> UUID:
    """Idempotent: returns the existing project id if ``slug`` already exists."""
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO projects (slug) VALUES (%s) ON CONFLICT (slug) DO NOTHING RETURNING id", (slug,)
        ).fetchone()
        if row is None:
            return conn.execute("SELECT id FROM projects WHERE slug = %s", (slug,)).fetchone()["id"]
        pid = row["id"]
        set_id = conn.execute(
            "INSERT INTO skill_sets (project_id, version, predecessor_id, content_hash, contains_mock_validation,"
            " created_by, reason) VALUES (%s, 0, NULL, %s, false, 'system', 'genesis (empty set)') RETURNING id",
            (pid, EMPTY_SET_HASH),
        ).fetchone()["id"]
        act_id = conn.execute(
            "INSERT INTO activations (project_id, idempotency_key, kind, new_set_id, actor, reason)"
            " VALUES (%s, %s, 'genesis', %s, 'system', 'project created') RETURNING id",
            (pid, f"genesis:{pid}", set_id),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO active_skill_sets (project_id, skill_set_id, activation_id) VALUES (%s, %s, %s)",
            (pid, set_id, act_id),
        )
        conn.execute(
            "INSERT INTO audit_events (project_id, actor, action, target, reason)"
            " VALUES (%s, 'system', 'project.create', %s, 'project created with empty genesis skill set')",
            (pid, Jsonb({"skill_set_id": str(set_id), "activation_id": str(act_id)})),
        )
        return pid


def project_id(conn: psycopg.Connection, slug: str) -> UUID:
    row = conn.execute("SELECT id FROM projects WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise LookupError(f"unknown project {slug!r}")
    return row["id"]


def active_skill_set_id(conn: psycopg.Connection, pid: UUID) -> UUID:
    return conn.execute("SELECT skill_set_id FROM active_skill_sets WHERE project_id = %s", (pid,)).fetchone()[
        "skill_set_id"
    ]

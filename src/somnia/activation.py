"""Transactional, idempotent, auditable skill-set activation and rollback."""

from __future__ import annotations

from typing import Callable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .contracts import ActivationResult
from .errors import IdempotencyConflict, IneligibleCandidate, StaleActivation, StaleParentVersion, translating
from .skills import load_candidate, set_members, skill_set_content_hash


def _replay(conn: psycopg.Connection, idempotency_key: str, *, gate_decision_id: UUID | None,
            target_set_id: UUID | None = None) -> ActivationResult | None:
    a = conn.execute(
        "SELECT a.id, a.gate_decision_id, a.previous_set_id, a.new_set_id, s.version FROM activations a"
        " JOIN skill_sets s ON s.id = a.new_set_id WHERE a.idempotency_key = %s", (idempotency_key,),
    ).fetchone()
    if a is None:
        return None
    if gate_decision_id is not None and a["gate_decision_id"] != gate_decision_id:
        raise IdempotencyConflict(f"activation key {idempotency_key!r} was used for a different gate decision")
    if target_set_id is not None and a["new_set_id"] != target_set_id:
        raise IdempotencyConflict(f"rollback key {idempotency_key!r} was used for a different target")
    return ActivationResult(activation_id=a["id"], previous_set_id=a["previous_set_id"], new_set_id=a["new_set_id"],
                            new_set_version=a["version"], applied=False)


def _audit_rejection(conn: psycopg.Connection, project_id: UUID, actor: str, target: dict, reason: str,
                     correlation_id: str) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO audit_events (project_id, actor, action, target, reason, correlation_id)"
            " VALUES (%s, %s, 'skill_set.activate.rejected', %s, %s, %s)",
            (project_id, actor, Jsonb(target), reason, correlation_id),
        )


def activate_candidate(conn: psycopg.Connection, *, project_id: UUID, gate_decision_id: UUID,
                       expected_previous_set_id: UUID, actor: str, idempotency_key: str, reason: str,
                       allow_mock_validation: bool, run_id: UUID | None = None,
                       guard: Callable[[psycopg.Connection], None] | None = None) -> ActivationResult:
    """Create a new skill set = current active set with the candidate added/replacing its parent,
    and move the active pointer — atomically, only if:
      * the gate decision passed and its candidate hash matches the stored, re-verified content;
      * mock validation is explicitly allowed when the gate is a mock;
      * the active set is still ``expected_previous_set_id`` (else StaleActivation);
      * the candidate's parent is the skill's currently active version, or the skill is absent
        when the candidate has no parent (else StaleParentVersion).
    Replaying the same ``idempotency_key`` returns the original result without changes.
    ``guard`` runs first inside the transaction (the sleep engine passes its lease check)."""
    try:
        with translating(), conn.transaction():
            if guard is not None:
                guard(conn)
            ptr = conn.execute("SELECT skill_set_id FROM active_skill_sets WHERE project_id = %s FOR UPDATE",
                               (project_id,)).fetchone()
            # Re-check idempotency only after taking the pointer lock, so a concurrent identical
            # request that just committed is seen as a replay rather than a stale conflict.
            replay = _replay(conn, idempotency_key, gate_decision_id=gate_decision_id)
            if replay is not None:
                return replay
            g = conn.execute("SELECT * FROM gate_decisions WHERE id = %s", (gate_decision_id,)).fetchone()
            if g is None:
                raise LookupError(f"unknown gate decision {gate_decision_id}")
            cand = load_candidate(conn, g["skill_version_id"])  # re-verifies content hash
            if g["candidate_hash"] != cand.content_hash:
                raise IneligibleCandidate("gate decision does not refer to the candidate's current content hash")
            if g["decision"] != "pass":
                raise IneligibleCandidate(f"gate decision is {g['decision']!r}, not 'pass'")
            if g["is_mock"] and not allow_mock_validation:
                raise IneligibleCandidate("gate decision is a mock; activation requires allow_mock_validation")
            current = ptr["skill_set_id"]
            if current != expected_previous_set_id:
                raise StaleActivation(f"active set is {current}, expected {expected_previous_set_id}")
            members = set_members(conn, current)
            by_skill = {m["skill_id"]: m for m in members}
            active_version = by_skill.get(cand.skill_id, {}).get("skill_version_id")
            if active_version != cand.parent_version_id:
                raise StaleParentVersion(
                    f"candidate revises {cand.parent_version_id}, but the active version of "
                    f"{cand.body.slug!r} is {active_version}")
            by_skill[cand.skill_id] = {"skill_id": cand.skill_id, "skill_version_id": cand.skill_version_id,
                                       "gate_decision_id": gate_decision_id, "gate_is_mock": g["is_mock"]}
            new_members = list(by_skill.values())
            cur_set = conn.execute("SELECT version FROM skill_sets WHERE id = %s", (current,)).fetchone()
            new_set = conn.execute(
                "INSERT INTO skill_sets (project_id, version, predecessor_id, content_hash, contains_mock_validation,"
                " created_by, reason) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, version",
                (project_id, cur_set["version"] + 1, current, skill_set_content_hash(new_members),
                 any(m["gate_is_mock"] for m in new_members), actor, reason),
            ).fetchone()
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO skill_set_members (skill_set_id, skill_id, skill_version_id, gate_decision_id)"
                    " VALUES (%s, %s, %s, %s)",
                    [(new_set["id"], m["skill_id"], m["skill_version_id"], m["gate_decision_id"]) for m in new_members],
                )
            act = conn.execute(
                "INSERT INTO activations (project_id, idempotency_key, kind, gate_decision_id, skill_version_id,"
                " expected_previous_set_id, previous_set_id, new_set_id, actor, reason, run_id)"
                " VALUES (%s, %s, 'activate', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (project_id, idempotency_key, gate_decision_id, cand.skill_version_id, expected_previous_set_id,
                 current, new_set["id"], actor, reason, run_id),
            ).fetchone()
            conn.execute("UPDATE active_skill_sets SET skill_set_id = %s, activation_id = %s,"
                         " updated_at = clock_timestamp() WHERE project_id = %s", (new_set["id"], act["id"], project_id))
            conn.execute(
                "INSERT INTO audit_events (project_id, actor, action, target, reason, correlation_id)"
                " VALUES (%s, %s, 'skill_set.activate', %s, %s, %s)",
                (project_id, actor, Jsonb({"previous_set_id": str(current), "new_set_id": str(new_set["id"]),
                                           "skill_version_id": str(cand.skill_version_id),
                                           "gate_decision_id": str(gate_decision_id),
                                           "gate_is_mock": g["is_mock"]}), reason, idempotency_key),
            )
            return ActivationResult(activation_id=act["id"], previous_set_id=current, new_set_id=new_set["id"],
                                    new_set_version=new_set["version"], applied=True)
    except (StaleActivation, StaleParentVersion, IneligibleCandidate) as exc:
        _audit_rejection(conn, project_id, actor, {"gate_decision_id": str(gate_decision_id),
                                                   "expected_previous_set_id": str(expected_previous_set_id)},
                         f"{type(exc).__name__}: {exc}", idempotency_key)
        raise


def rollback_to(conn: psycopg.Connection, *, project_id: UUID, target_set_id: UUID, expected_current_set_id: UUID,
                actor: str, idempotency_key: str, reason: str) -> ActivationResult:
    """Point the project back at a known earlier set. History is kept; nothing is deleted."""
    with translating(), conn.transaction():
        ptr = conn.execute("SELECT skill_set_id FROM active_skill_sets WHERE project_id = %s FOR UPDATE",
                           (project_id,)).fetchone()
        replay = _replay(conn, idempotency_key, gate_decision_id=None, target_set_id=target_set_id)
        if replay is not None:
            return replay
        if ptr["skill_set_id"] != expected_current_set_id:
            raise StaleActivation(f"active set is {ptr['skill_set_id']}, expected {expected_current_set_id}")
        tgt = conn.execute("SELECT id, version FROM skill_sets WHERE id = %s AND project_id = %s",
                           (target_set_id, project_id)).fetchone()
        if tgt is None:
            raise LookupError("rollback target is not a skill set of this project")
        act = conn.execute(
            "INSERT INTO activations (project_id, idempotency_key, kind, expected_previous_set_id, previous_set_id,"
            " new_set_id, actor, reason) VALUES (%s, %s, 'rollback', %s, %s, %s, %s, %s) RETURNING id",
            (project_id, idempotency_key, expected_current_set_id, ptr["skill_set_id"], target_set_id, actor, reason),
        ).fetchone()
        conn.execute("UPDATE active_skill_sets SET skill_set_id = %s, activation_id = %s,"
                     " updated_at = clock_timestamp() WHERE project_id = %s", (target_set_id, act["id"], project_id))
        conn.execute(
            "INSERT INTO audit_events (project_id, actor, action, target, reason, correlation_id)"
            " VALUES (%s, %s, 'skill_set.rollback', %s, %s, %s)",
            (project_id, actor, Jsonb({"previous_set_id": str(ptr["skill_set_id"]),
                                       "new_set_id": str(target_set_id)}), reason, idempotency_key),
        )
        return ActivationResult(activation_id=act["id"], previous_set_id=ptr["skill_set_id"], new_set_id=target_set_id,
                                new_set_version=tgt["version"], applied=True)

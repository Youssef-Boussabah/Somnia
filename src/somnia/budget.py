"""Transactional budget ledger.

Amounts are integer micro-units. Two units exist and never mix:
  CAD_MICRO  – real money (1 CAD = 1_000_000). No real provider exists in this assignment.
  MOCK_MICRO – fake units used to exercise the ledger with mock calls. Not money.

Invariants enforced inside PostgreSQL (migration 0001), not only here:
  * reservations hold funds on the leaf envelope and every ancestor; a reservation that does
    not fit anywhere in the chain is rejected atomically (SB001), also under concurrency;
  * reserved -> settled | released | uncertain, uncertain -> settled; nothing else (SB002);
  * an uncertain reservation keeps holding funds until reconciled with an explicit actual cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .errors import IdempotencyConflict, InvalidReservationTransition, translating

MICRO = 1_000_000


@dataclass(frozen=True)
class Reservation:
    id: UUID
    envelope_id: UUID
    amount_micro: int
    state: str
    created: bool


def create_envelope(conn: psycopg.Connection, project_id: UUID, name: str, *, unit: str, limit_micro: int,
                    parent_id: UUID | None = None) -> UUID:
    """Idempotent by (project, name) when parameters match."""
    with translating(), conn.transaction():
        row = conn.execute(
            "INSERT INTO budget_envelopes (project_id, parent_id, name, unit, limit_micro)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (project_id, name) DO NOTHING RETURNING id",
            (project_id, parent_id, name, unit, limit_micro),
        ).fetchone()
        if row is not None:
            return row["id"]
        ex = conn.execute(
            "SELECT id, parent_id, unit, limit_micro FROM budget_envelopes WHERE project_id = %s AND name = %s",
            (project_id, name),
        ).fetchone()
        if (ex["parent_id"], ex["unit"], ex["limit_micro"]) != (parent_id, unit, limit_micro):
            raise IdempotencyConflict(f"envelope {name!r} exists with different parameters")
        return ex["id"]


def envelope_by_name(conn: psycopg.Connection, project_id: UUID, name: str) -> UUID:
    row = conn.execute("SELECT id FROM budget_envelopes WHERE project_id = %s AND name = %s",
                       (project_id, name)).fetchone()
    if row is None:
        raise LookupError(f"unknown envelope {name!r}")
    return row["id"]


def reserve(conn: psycopg.Connection, *, envelope_id: UUID, amount_micro: int, idempotency_key: str, purpose: str,
            provider: str, model: str, pricing_snapshot: dict[str, Any], run_id: UUID | None = None,
            stage_attempt_id: UUID | None = None,
            guard: Callable[[psycopg.Connection], None] | None = None) -> Reservation:
    """Atomically reserve an upper-bound cost. ``guard`` runs inside the same transaction (e.g. a
    lease check) so an expired worker cannot reserve. Raises BudgetExceeded if it does not fit."""
    with translating(), conn.transaction():
        if guard is not None:
            guard(conn)
        row = conn.execute(
            "INSERT INTO reservations (envelope_id, idempotency_key, run_id, stage_attempt_id, purpose, provider,"
            " model, amount_micro, pricing_snapshot, state)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved')"
            " ON CONFLICT (idempotency_key) DO NOTHING RETURNING id, state",
            (envelope_id, idempotency_key, run_id, stage_attempt_id, purpose, provider, model, amount_micro,
             Jsonb(pricing_snapshot)),
        ).fetchone()
        if row is not None:
            return Reservation(row["id"], envelope_id, amount_micro, row["state"], True)
        ex = conn.execute(
            "SELECT id, envelope_id, amount_micro, state FROM reservations WHERE idempotency_key = %s",
            (idempotency_key,),
        ).fetchone()
        if (ex["envelope_id"], ex["amount_micro"]) != (envelope_id, amount_micro):
            raise IdempotencyConflict(f"reservation key {idempotency_key!r} reused with different parameters")
        return Reservation(ex["id"], ex["envelope_id"], ex["amount_micro"], ex["state"], False)


def _transition(conn: psycopg.Connection, reservation_id: UUID, *, to_state: str, allowed_from: tuple[str, ...],
                actual_micro: int | None, request_id: str | None, note: str | None) -> None:
    with translating(), conn.transaction():
        cur = conn.execute("SELECT state FROM reservations WHERE id = %s FOR UPDATE", (reservation_id,)).fetchone()
        if cur is None:
            raise LookupError(f"unknown reservation {reservation_id}")
        if cur["state"] not in allowed_from:
            raise InvalidReservationTransition(f"reservation {reservation_id} is {cur['state']}; cannot -> {to_state}")
        conn.execute(
            "UPDATE reservations SET state = %s, actual_micro = %s, request_id = COALESCE(%s, request_id),"
            " resolution_note = COALESCE(%s, resolution_note) WHERE id = %s",
            (to_state, actual_micro, request_id, note, reservation_id),
        )


def settle(conn: psycopg.Connection, reservation_id: UUID, *, actual_micro: int, request_id: str) -> None:
    """Record the provider-reported cost of a completed call and free the hold."""
    _transition(conn, reservation_id, to_state="settled", allowed_from=("reserved",), actual_micro=actual_micro,
                request_id=request_id, note=None)


def release_unsent(conn: psycopg.Connection, reservation_id: UUID, *, note: str) -> None:
    """Free a hold only when the request was definitively never accepted by the provider."""
    _transition(conn, reservation_id, to_state="released", allowed_from=("reserved",), actual_micro=None,
                request_id=None, note=note)


def mark_uncertain(conn: psycopg.Connection, reservation_id: UUID, *, note: str) -> None:
    """The call may have been billed. The hold is retained until ``reconcile``."""
    _transition(conn, reservation_id, to_state="uncertain", allowed_from=("reserved",), actual_micro=None,
                request_id=None, note=note)


def reconcile(conn: psycopg.Connection, reservation_id: UUID, *, actual_micro: int, note: str,
              request_id: str | None = None) -> None:
    """Operator resolution of an uncertain reservation with an evidenced actual cost (0 allowed)."""
    if not note.strip():
        raise ValueError("reconciliation requires a note describing the billing evidence")
    _transition(conn, reservation_id, to_state="settled", allowed_from=("uncertain",), actual_micro=actual_micro,
                request_id=request_id, note=note)


def envelope_status(conn: psycopg.Connection, envelope_id: UUID) -> dict[str, Any]:
    r = conn.execute(
        "SELECT id, name, unit, limit_micro, reserved_micro, confirmed_micro FROM budget_envelopes WHERE id = %s",
        (envelope_id,),
    ).fetchone()
    return {**r, "available_micro": r["limit_micro"] - r["reserved_micro"] - r["confirmed_micro"]}


def ledger_mismatches(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    """Recompute each envelope's counters from the reservation ledger (including descendants).
    Returns envelopes whose stored counters disagree; empty means consistent."""
    return conn.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id AS root, id AS node FROM budget_envelopes WHERE project_id = %(p)s
            UNION ALL
            SELECT t.root, c.id FROM tree t JOIN budget_envelopes c ON c.parent_id = t.node
        ), sums AS (
            SELECT t.root,
                   COALESCE(SUM(r.amount_micro) FILTER (WHERE r.state IN ('reserved','uncertain')), 0) AS held,
                   COALESCE(SUM(r.actual_micro) FILTER (WHERE r.state = 'settled'), 0) AS confirmed
              FROM tree t LEFT JOIN reservations r ON r.envelope_id = t.node
             GROUP BY t.root
        )
        SELECT e.id, e.name, e.reserved_micro, s.held, e.confirmed_micro, s.confirmed
          FROM budget_envelopes e JOIN sums s ON s.root = e.id
         WHERE e.reserved_micro <> s.held OR e.confirmed_micro <> s.confirmed
        """,
        {"p": project_id},
    ).fetchall()


def spend_summary(conn: psycopg.Connection, project_id: UUID) -> dict[str, dict[str, int]]:
    """Totals per unit across root envelopes only (children roll up into roots)."""
    rows = conn.execute(
        "SELECT unit, SUM(limit_micro) AS limit_micro, SUM(reserved_micro) AS held_micro,"
        " SUM(confirmed_micro) AS confirmed_micro FROM budget_envelopes"
        " WHERE project_id = %s AND parent_id IS NULL GROUP BY unit", (project_id,),
    ).fetchall()
    out = {u: {"limit_micro": 0, "held_micro": 0, "confirmed_micro": 0} for u in ("CAD_MICRO", "MOCK_MICRO")}
    for r in rows:
        out[r["unit"]] = {k: int(r[k]) for k in ("limit_micro", "held_micro", "confirmed_micro")}
    return out


def outstanding(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT r.id, r.state, r.amount_micro, e.unit, e.name AS envelope, r.purpose, r.run_id, r.created_at"
        " FROM reservations r JOIN budget_envelopes e ON e.id = r.envelope_id"
        " WHERE e.project_id = %s AND r.state IN ('reserved', 'uncertain') ORDER BY r.created_at", (project_id,),
    ).fetchall()

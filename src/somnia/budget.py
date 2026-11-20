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
    """BILLING resolution of an uncertain reservation with an evidenced actual cost (0 allowed).

    This settles what the hold cost; it says nothing about whether the request completed. It does
    NOT authorize sending the request again (D044): a later attempt is blocked as RequestOutstanding
    until the response is recorded or the request is abandoned with evidence (``abandon_request``).
    If the response arrives later with a different reported usage, a billing discrepancy is opened.

    A concurrent abandonment is serialized with this on the hold's row lock (the abandonment trigger takes it
    FOR UPDATE), so it either commits first and no residual hold is created, or waits and then releases the
    residual this creates. Neither ordering strands a hold (D048)."""
    if not note.strip():
        raise ValueError("reconciliation requires a note describing the billing evidence")
    _transition(conn, reservation_id, to_state="settled", allowed_from=("uncertain",), actual_micro=actual_micro,
                request_id=request_id, note=note)


def abandon_request(conn: psycopg.Connection, reservation_id: UUID, *, evidence: str, actor: str,
                    sender_stopped_evidence: str | None = None) -> None:
    """REQUEST resolution: record that the request sent under ``reservation_id`` will never produce a
    response, which is the only thing that permits a retry when no response arrived (BUDGET.md, D046).

    Runs under the logical-request lock. Allowed only after the hold was marked uncertain (or reconciled),
    never while it is 'reserved', and never once a response is recorded. What is required depends on the
    send permit (``provider_request_sends``), which a sender must take, fenced by its lease, immediately
    before contacting the provider:
      * no permit: nothing was sent, and the original sender can never obtain one later (its lease is
        gone and the request is abandoned), so provider-side ``evidence`` suffices;
      * permit whose send has ended (the provider call returned, e.g. with an unknown outcome): the local
        sender is done; provider-side ``evidence`` that the request failed or never completed is required;
      * permit still OPEN: the sender may be paused between its permit and the network write, or the
        request may be in flight. A provider log showing no request *yet* proves nothing about that, so
        ``sender_stopped_evidence`` is also required (e.g. the worker process is confirmed terminated).
    Acceptable provider-side evidence: the provider's request log/dashboard showing no request with this
    request id or idempotency key, a provider error/status record showing it failed or was cancelled, or
    provider support confirmation. Abandoning releases the request's residual hold. A response that still
    arrives is recorded, and opens a discrepancy if billing differs."""
    if len(evidence.strip()) < 10:
        raise ValueError("abandonment requires a description of the provider-side evidence")
    with translating(), conn.transaction():
        q = conn.execute("SELECT run_id, request_key FROM provider_requests WHERE reservation_id = %s",
                         (reservation_id,)).fetchone()
        if q is None:
            raise LookupError(f"no recorded provider request for reservation {reservation_id}")
        conn.execute("SELECT lock_logical_request(%s, %s)", (q["run_id"], q["request_key"]))
        conn.execute("INSERT INTO provider_request_abandonments (reservation_id, evidence, sender_stopped_evidence,"
                     " actor) VALUES (%s, %s, %s, %s)", (reservation_id, evidence, sender_stopped_evidence, actor))


def resolve_discrepancy(conn: psycopg.Connection, discrepancy_id: UUID, *, note: str, actor: str) -> None:
    """Acknowledge an open billing discrepancy and lift the spending freeze on its envelope tree.
    The difference was already applied to the ledger when the discrepancy was opened; resolving never
    applies it again. Both the operator's reconciliation and the provider-reported usage stay on record."""
    if not note.strip():
        raise ValueError("resolving a billing discrepancy requires a note")
    with translating(), conn.transaction():
        row = conn.execute("UPDATE billing_discrepancies SET status = 'resolved', resolution_note = %s, resolved_by = %s,"
                           " resolved_at = clock_timestamp() WHERE id = %s AND status = 'open' RETURNING id",
                           (note, actor, discrepancy_id)).fetchone()
        if row is None:
            raise InvalidReservationTransition(f"billing discrepancy {discrepancy_id} is not open")


def open_discrepancies(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT d.id, d.reservation_id, d.provider_call_id, e.name AS envelope, d.envelope_id, d.root_envelope_id,"
        " d.operator_micro, d.reported_micro, d.delta_micro, d.created_at FROM billing_discrepancies d"
        " JOIN budget_envelopes e ON e.id = d.envelope_id WHERE e.project_id = %s AND d.status = 'open'"
        " ORDER BY d.created_at", (project_id,)).fetchall()


def outstanding_requests(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    """Requests that were (or may have been) sent and have no response, release or abandonment."""
    return conn.execute(
        "SELECT q.reservation_id, q.run_id, q.request_key, r.state AS hold_state, r.actual_micro, q.created_at,"
        " (SELECT h.amount_micro FROM request_residual_holds h WHERE h.reservation_id = q.reservation_id"
        "   AND h.released_at IS NULL) AS residual_micro,"
        " (SELECT CASE WHEN s.ended_at IS NULL THEN 'open' ELSE 'ended' END FROM provider_request_sends s"
        "   WHERE s.reservation_id = q.reservation_id) AS send_permit,"
        " (SELECT s.outcome FROM provider_request_sends s WHERE s.reservation_id = q.reservation_id) AS send_outcome"
        " FROM provider_requests q JOIN reservations r ON r.id = q.reservation_id"
        " JOIN budget_envelopes e ON e.id = r.envelope_id WHERE e.project_id = %s AND r.state <> 'released'"
        " AND NOT EXISTS (SELECT 1 FROM provider_calls c WHERE c.reservation_id = q.reservation_id)"
        " AND NOT EXISTS (SELECT 1 FROM provider_request_abandonments a WHERE a.reservation_id = q.reservation_id)"
        " ORDER BY q.created_at", (project_id,)).fetchall()


def alerts(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    """Conditions an operator must see: confirmed spend above a limit (evidence is never trimmed to fit),
    open billing discrepancies (spending frozen) and outstanding requests (no resend allowed)."""
    out: list[dict[str, Any]] = [
        {"kind": "overrun", "envelope": r["name"], "envelope_id": str(r["id"]), "unit": r["unit"],
         "limit_micro": r["limit_micro"], "confirmed_micro": r["confirmed_micro"], "held_micro": r["reserved_micro"]}
        for r in conn.execute("SELECT id, name, unit, limit_micro, confirmed_micro, reserved_micro FROM budget_envelopes"
                              " WHERE project_id = %s AND confirmed_micro > limit_micro ORDER BY name",
                              (project_id,)).fetchall()]
    out += [{"kind": "open_discrepancy", "discrepancy_id": str(d["id"]), "envelope": d["envelope"],
             "operator_micro": d["operator_micro"], "reported_micro": d["reported_micro"],
             "delta_micro": d["delta_micro"]} for d in open_discrepancies(conn, project_id)]
    out += [{"kind": "outstanding_request", "reservation_id": str(q["reservation_id"]), "run_id": str(q["run_id"]),
             "hold_state": q["hold_state"], "residual_micro": q["residual_micro"], "send_permit": q["send_permit"],
             "send_outcome": q["send_outcome"]}
            for q in outstanding_requests(conn, project_id)]
    return out


def envelope_status(conn: psycopg.Connection, envelope_id: UUID) -> dict[str, Any]:
    r = conn.execute(
        "SELECT id, name, unit, limit_micro, reserved_micro, confirmed_micro FROM budget_envelopes WHERE id = %s",
        (envelope_id,),
    ).fetchone()
    return {**r, "available_micro": r["limit_micro"] - r["reserved_micro"] - r["confirmed_micro"]}


def ledger_mismatches(conn: psycopg.Connection, project_id: UUID) -> list[dict[str, Any]]:
    """Recompute each envelope's counters from the ledger (including descendants): held = reserved and
    uncertain reservations + open residual holds; confirmed = settled actuals + billing-discrepancy
    differences (each once).
    Returns envelopes whose stored counters disagree; empty means consistent."""
    return conn.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id AS root, id AS node FROM budget_envelopes WHERE project_id = %(p)s
            UNION ALL
            SELECT t.root, c.id FROM tree t JOIN budget_envelopes c ON c.parent_id = t.node
        ), res AS (
            SELECT t.root,
                   COALESCE(SUM(r.amount_micro) FILTER (WHERE r.state IN ('reserved','uncertain')), 0) AS held,
                   COALESCE(SUM(r.actual_micro) FILTER (WHERE r.state = 'settled'), 0) AS settled
              FROM tree t LEFT JOIN reservations r ON r.envelope_id = t.node
             GROUP BY t.root
        ), adj AS (
            SELECT t.root, COALESCE(SUM(d.delta_micro), 0) AS delta
              FROM tree t LEFT JOIN billing_discrepancies d ON d.envelope_id = t.node
             GROUP BY t.root
        ), resid AS (
            SELECT t.root, COALESCE(SUM(h.amount_micro) FILTER (WHERE h.released_at IS NULL), 0) AS held
              FROM tree t LEFT JOIN request_residual_holds h ON h.envelope_id = t.node
             GROUP BY t.root
        ), sums AS (
            SELECT res.root, res.held + resid.held AS held, res.settled + adj.delta AS confirmed
              FROM res JOIN adj USING (root) JOIN resid USING (root)
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

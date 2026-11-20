"""The single budget-guarded path for model calls.

Every call has two separate durable facts (DECISIONS.md D044, D046; BUDGET.md):
  * billing -- the reservation: reserved -> settled | released | uncertain; uncertain -> settled (reconcile);
  * request -- a ``provider_requests`` row written with the reservation, BEFORE the send (identity + prompt).
    It is completed when a ``provider_calls`` row exists; it is resolved without a response only by an
    evidence-backed abandonment (``budget.abandon_request``).

All decisions about one LOGICAL request (run, request key) take the same transaction-scoped lock
(``lock_logical_request``): admission (reuse / block / reserve), the send permit, recording a response and
abandonment. So a response that commits while a replacement is deciding is either seen (reused) or waits
until the replacement has decided; it can never slip between two checks.

``ModelGateway.call``:
  1. fast path: a completed call for the logical request is reused, never re-sent;
  2. admission, under the lock (and the lease guard): re-check for a completed call (reuse); pause if the
     only receipt is operator-recorded -> ``OperatorReceiptOnly`` (D049); refuse if an
     earlier send is OUTSTANDING (no response, not released, not abandoned) -> ``RequestOutstanding``;
     otherwise reserve the upper bound and record the request identity;
  3. send permit, under the lock and the lease guard: refused if the lease was lost or the request was
     abandoned, so a sender that was fenced out (e.g. paused before sending) can never send later;
  4. send: the permit is ended in every case once the provider call has returned, with its outcome. While the
     hold is still 'reserved': definitively not sent -> release; outcome unknown -> 'uncertain', BillingUncertain.
     If a successor or operator already took the hold over, its billing is left untouched (D048);
  5. ``record_response`` persists the response exactly once in whatever billing state the hold is by then.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

import psycopg

from . import budget
from .artifacts import ArtifactStore
from .contracts import GatewayConfig
from .errors import (BillingUncertain, IntegrityFailure, InvalidRequestResolution, LeaseLost, OperatorReceiptOnly,
                     RequestOutstanding, translating)
from .hashing import hash_obj, sha256_text
from .interfaces import ModelProvider, ProviderNotSent, ProviderResponse

Hook = Callable[[str, dict], None]


@dataclass(frozen=True)
class CallContext:
    run_id: UUID
    stage: str
    stage_attempt_id: UUID
    envelope_id: UUID
    guard: Callable[[psycopg.Connection], None]
    fence: int | None = None
    worker_id: str | None = None


@dataclass(frozen=True)
class GatewayResult:
    text: str
    provider_call_id: UUID
    reused: bool


@dataclass(frozen=True)
class CompletionReceipt:
    """Durable receipt for a recorded response. ``created`` is False for a duplicate delivery;
    ``discrepancy_id`` is set only when THIS delivery opened a billing discrepancy."""
    provider_call_id: UUID
    created: bool
    reported_micro: int
    discrepancy_id: UUID | None


def lock_logical_request(conn: psycopg.Connection, run_id: UUID | None, request_key: str) -> None:
    """Serialize every decision about one logical request until the end of the current transaction."""
    conn.execute("SELECT lock_logical_request(%s, %s)", (run_id, request_key))


def _completed_call(conn: psycopg.Connection, run_id: UUID, request_key: str) -> dict[str, Any] | None:
    """The gateway's own receipt for the logical request. Operator-recorded responses complete their request
    and count for billing, but are never reused as model output (D048)."""
    return conn.execute("SELECT c.id, c.response_artifact FROM provider_calls c WHERE c.run_id = %s"
                        " AND c.request_key = %s AND NOT EXISTS (SELECT 1 FROM operator_recorded_responses o"
                        " WHERE o.provider_call_id = c.id) ORDER BY c.created_at LIMIT 1",
                        (run_id, request_key)).fetchone()


def _operator_receipt(conn: psycopg.Connection, run_id: UUID, request_key: str) -> dict[str, Any] | None:
    """An operator-recorded receipt for the logical request (D048/D049)."""
    return conn.execute("SELECT c.id, c.reservation_id FROM provider_calls c JOIN operator_recorded_responses o"
                        " ON o.provider_call_id = c.id WHERE c.run_id = %s AND c.request_key = %s"
                        " ORDER BY c.created_at LIMIT 1", (run_id, request_key)).fetchone()


def outstanding_request(conn: psycopg.Connection, run_id: UUID, request_key: str) -> dict[str, Any] | None:
    """An earlier send of this request that may still produce (or may already have billed) a response."""
    return conn.execute(
        "SELECT q.reservation_id, r.state FROM provider_requests q JOIN reservations r ON r.id = q.reservation_id"
        " WHERE q.run_id = %s AND q.request_key = %s AND r.state <> 'released'"
        " AND NOT EXISTS (SELECT 1 FROM provider_calls c WHERE c.reservation_id = q.reservation_id)"
        " AND NOT EXISTS (SELECT 1 FROM provider_request_abandonments a WHERE a.reservation_id = q.reservation_id)"
        " ORDER BY q.created_at LIMIT 1", (run_id, request_key)).fetchone()


def reported_usage_micro(pricing_snapshot: dict[str, Any], resp: ProviderResponse) -> int:
    """Cost of the provider-reported usage at the pricing recorded with the reservation."""
    return (resp.input_tokens * int(pricing_snapshot["input_micro_per_token"])
            + resp.output_tokens * int(pricing_snapshot["output_micro_per_token"]))


@dataclass(frozen=True)
class OperatorRecord:
    """Who recorded a response that reached the operator instead of the gateway, and on what evidence."""
    actor: str
    evidence: str


def record_provider_error(conn: psycopg.Connection, reservation_id: UUID, *, outcome: str, note: str) -> str:
    """The sender's provider call returned an error. Ends the send permit with ``outcome`` ('not_sent' or
    'outcome_unknown') and returns the hold's billing state afterwards. No lease is required: this is evidence.

    Billing is changed only while the hold is still 'reserved' (its owner never lost it): not_sent releases it,
    outcome_unknown marks it uncertain. Once a successor marked it uncertain, or an operator reconciled it,
    the billing record, the residual hold and the request's outstanding status are left exactly as they are:
    the error is recorded on the permit for the operator, and an unknown outcome (e.g. a timeout) never
    resolves the request, so it never authorizes another send (D048)."""
    if outcome not in ("not_sent", "outcome_unknown"):
        raise ValueError(f"unknown provider error outcome {outcome!r}")
    with translating(), conn.transaction():
        ident = conn.execute("SELECT run_id, request_key FROM provider_requests WHERE reservation_id = %s",
                             (reservation_id,)).fetchone()
        lock_logical_request(conn, ident["run_id"], ident["request_key"])
        state = conn.execute("SELECT state FROM reservations WHERE id = %s FOR UPDATE",
                             (reservation_id,)).fetchone()["state"]
        conn.execute("UPDATE provider_request_sends SET ended_at = clock_timestamp(), outcome = %s, end_note = %s"
                     " WHERE reservation_id = %s AND ended_at IS NULL", (outcome, note, reservation_id))
        if state == "reserved":
            if outcome == "not_sent":
                budget.release_unsent(conn, reservation_id, note=f"provider rejected before acceptance: {note}")
                return "released"
            budget.mark_uncertain(conn, reservation_id, note=note)
            return "uncertain"
        return state


def record_response(conn: psycopg.Connection, artifacts: ArtifactStore, reservation_id: UUID,
                    resp: ProviderResponse, *, operator: OperatorRecord | None = None) -> CompletionReceipt:
    """Persist a provider response for a recorded request exactly once, in any billing state.

    No lease is required: a worker whose lease expired still holds the evidence and must record it (it remains
    unable to commit the stage). Runs under the logical-request lock. Recording ends the send permit and
    releases any residual hold (database triggers). Outcomes by the hold's billing state:
      reserved  -> settle with the reported usage (the normal path);
      uncertain -> reconcile with the reported usage (the hold was orphaned while the call was in flight);
      settled / released (an operator reconciled first, or the hold was released) -> keep the operator's
        record; if its amount differs from the reported usage, open a billing discrepancy, which applies
        the difference to the ledger once and freezes new spending in the envelope tree until resolved.
    A duplicate delivery of the same response returns the existing receipt and changes nothing; a
    different response for the same request raises IntegrityFailure and changes nothing.

    ``operator`` marks a response that reached an operator rather than the gateway (``budget record-response``).
    It is required for a request created before 0003, whose prompt was never stored: the call is then recorded
    with no prompt ("prompt not recorded") instead of an invented one. Operator-recorded responses settle
    billing and complete the request exactly like the gateway's, but are never reused as model output (D048).

    Reported token counts must each be nonnegative (zero is valid). A negative count is rejected here, before
    anything is written -- even when the weighted total would be zero or positive -- and again by PostgreSQL
    (D049)."""
    for name, value in (("input_tokens", resp.input_tokens), ("output_tokens", resp.output_tokens)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"reported {name} must be a nonnegative integer, got {value!r}; nothing was recorded")
    with translating(), conn.transaction():
        ident = conn.execute("SELECT run_id, request_key FROM provider_requests WHERE reservation_id = %s",
                             (reservation_id,)).fetchone()
        if ident is None:
            raise LookupError(f"no recorded provider request for reservation {reservation_id}")
        lock_logical_request(conn, ident["run_id"], ident["request_key"])
        q = conn.execute(
            "SELECT q.*, r.state, r.actual_micro, r.envelope_id, r.pricing_snapshot FROM provider_requests q"
            " JOIN reservations r ON r.id = q.reservation_id WHERE q.reservation_id = %s FOR UPDATE OF r",
            (reservation_id,)).fetchone()
        reported = reported_usage_micro(q["pricing_snapshot"], resp)
        r_sha = artifacts.put(conn, resp.text.encode("utf-8"), media_type="application/json",
                              origin="mock" if q["is_mock"] else "observed")
        existing = conn.execute("SELECT id, response_artifact, input_tokens, output_tokens, request_id FROM provider_calls"
                                " WHERE reservation_id = %s", (reservation_id,)).fetchone()
        if existing is not None:
            same = (existing["response_artifact"], existing["input_tokens"], existing["output_tokens"],
                    existing["request_id"]) == (r_sha, resp.input_tokens, resp.output_tokens, resp.request_id)
            if not same:
                raise IntegrityFailure(f"a different response is already recorded for reservation {reservation_id}")
            return CompletionReceipt(existing["id"], False, reported, None)
        if q["prompt_artifact"] is None and operator is None:
            raise IntegrityFailure(f"request {reservation_id} predates prompt recording; an operator must record its"
                                   " response with `somnia budget record-response` (prompt stays unrecorded)")
        call_id = conn.execute(
            "INSERT INTO provider_calls (reservation_id, run_id, stage_attempt_id, request_key, provider, model,"
            " is_mock, prompt_artifact, response_artifact, input_tokens, output_tokens, request_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (reservation_id, q["run_id"], q["stage_attempt_id"], q["request_key"], q["provider"], q["model"],
             q["is_mock"], q["prompt_artifact"], r_sha, resp.input_tokens, resp.output_tokens, resp.request_id),
        ).fetchone()["id"]
        if operator is not None:
            conn.execute("INSERT INTO operator_recorded_responses (provider_call_id, reservation_id, actor, evidence,"
                         " prompt_recorded) VALUES (%s, %s, %s, %s, %s)",
                         (call_id, reservation_id, operator.actor, operator.evidence, q["prompt_artifact"] is not None))
        discrepancy = None
        if q["state"] == "reserved":
            budget.settle(conn, reservation_id, actual_micro=reported, request_id=resp.request_id)
        elif q["state"] == "uncertain":
            budget.reconcile(conn, reservation_id, actual_micro=reported, request_id=resp.request_id,
                             note="provider-reported usage recorded by the original caller after its lease was lost")
        elif (q["actual_micro"] or 0) != reported:
            discrepancy = conn.execute(
                "INSERT INTO billing_discrepancies (reservation_id, provider_call_id, envelope_id, root_envelope_id,"
                " operator_micro, reported_micro, delta_micro, status)"
                " VALUES (%s, %s, %s, envelope_root(%s), %s, %s, %s, 'open') RETURNING id",
                (reservation_id, call_id, q["envelope_id"], q["envelope_id"], q["actual_micro"] or 0, reported,
                 reported - (q["actual_micro"] or 0))).fetchone()["id"]
    return CompletionReceipt(call_id, True, reported, discrepancy)


class ModelGateway:
    def __init__(self, conn: psycopg.Connection, artifacts: ArtifactStore, provider: ModelProvider,
                 cfg: GatewayConfig, hook: Hook | None = None):
        self.conn, self.artifacts, self.provider, self.cfg = conn, artifacts, provider, cfg
        self._hook = hook or (lambda name, info: None)

    def upper_bound_micro(self) -> int:
        c = self.cfg
        return c.max_input_tokens * c.input_micro_per_token + c.max_output_tokens * c.output_micro_per_token

    def _raise_if_fenced_out(self, ctx: CallContext, exc: BaseException) -> None:
        """After the error is recorded: a worker that lost its lease reports LeaseLost, not an operator block."""
        with translating(), self.conn.transaction():
            try:
                ctx.guard(self.conn)
            except LeaseLost as lost:
                raise lost from exc

    def _reuse(self, prior: dict[str, Any]) -> GatewayResult:
        return GatewayResult(self.artifacts.get(prior["response_artifact"]).decode("utf-8"), prior["id"], True)

    def call(self, ctx: CallContext, template_id: str, prompt: str) -> GatewayResult:
        conn, cfg = self.conn, self.cfg
        request_key = hash_obj({"run": ctx.run_id, "stage": ctx.stage, "template": template_id,
                                "provider": self.provider.name, "model": cfg.model, "prompt": sha256_text(prompt)})
        prior = _completed_call(conn, ctx.run_id, request_key)  # fast path only; re-checked under the lock
        if prior is not None:
            return self._reuse(prior)
        self._hook("gateway.after_reuse_lookup", {"stage": ctx.stage, "request_key": request_key})

        env_unit = conn.execute("SELECT unit FROM budget_envelopes WHERE id = %s", (ctx.envelope_id,)).fetchone()["unit"]
        if not (self.provider.unit == cfg.unit == env_unit):
            raise ValueError(f"unit mismatch: provider {self.provider.unit}, config {cfg.unit}, envelope {env_unit}")
        if self.provider.is_mock != (cfg.unit == "MOCK_MICRO"):
            raise ValueError("mock providers must use MOCK_MICRO and real providers CAD_MICRO")
        in_tokens = self.provider.count_tokens(prompt)
        if in_tokens > cfg.max_input_tokens:
            raise ValueError(f"prompt ({in_tokens} tokens) exceeds the reserved input bound {cfg.max_input_tokens}")

        # ---- admission: one decision (reuse / block / reserve) per logical request
        with translating(), conn.transaction():
            ctx.guard(conn)  # an expired worker cannot reserve or send
            lock_logical_request(conn, ctx.run_id, request_key)
            prior = _completed_call(conn, ctx.run_id, request_key)
            if prior is None:
                self._hook("gateway.admission_after_completion_check", {"stage": ctx.stage, "request_key": request_key})
                op = _operator_receipt(conn, ctx.run_id, request_key)
                if op is not None:  # keep its billing and provenance; neither reuse it nor send again (D049)
                    raise OperatorReceiptOnly(
                        f"request {request_key[:16]}... has only an operator-recorded receipt (provider call"
                        f" {op['id']}, reservation {op['reservation_id']}); it is not reused as model output and"
                        " does not authorize another send. The run stays paused until a retry is authorized")
                pending = outstanding_request(conn, ctx.run_id, request_key)
                if pending is not None:
                    raise RequestOutstanding(
                        f"request {request_key[:16]}... was sent under reservation {pending['reservation_id']}"
                        f" (hold {pending['state']}) and has neither a recorded response nor an evidence-backed"
                        " abandonment; billing reconciliation alone does not permit another send")
                res = budget.reserve(
                    conn, envelope_id=ctx.envelope_id, amount_micro=self.upper_bound_micro(),
                    idempotency_key=f"{request_key}:{ctx.stage_attempt_id}", purpose=f"{ctx.stage}:{template_id}",
                    provider=self.provider.name, model=cfg.model,
                    pricing_snapshot={**cfg.model_dump(mode="json"), "bound": "max_input*in_rate + max_output*out_rate"},
                    run_id=ctx.run_id, stage_attempt_id=ctx.stage_attempt_id)
                if res.state != "reserved":
                    raise BillingUncertain(f"reservation {res.id} for this attempt is already {res.state}")
                if res.created:
                    p_sha = self.artifacts.put(conn, prompt.encode("utf-8"), media_type="text/plain",
                                               origin="mock" if self.provider.is_mock else "observed")
                    conn.execute(
                        "INSERT INTO provider_requests (reservation_id, run_id, stage_attempt_id, request_key, provider,"
                        " model, is_mock, prompt_artifact) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (res.id, ctx.run_id, ctx.stage_attempt_id, request_key, self.provider.name, cfg.model,
                         self.provider.is_mock, p_sha))
        if prior is not None:  # completed while we were deciding: reuse, no reservation was made
            return self._reuse(prior)
        self._hook("gateway.after_reserve", {"reservation_id": res.id, "stage": ctx.stage})

        # ---- send permit: fenced, immediately before contacting the provider
        with translating(), conn.transaction():
            ctx.guard(conn)  # LeaseLost if this worker was fenced out meanwhile: it never sends
            lock_logical_request(conn, ctx.run_id, request_key)
            if conn.execute("SELECT 1 FROM provider_request_abandonments WHERE reservation_id = %s",
                            (res.id,)).fetchone() is not None:
                raise InvalidRequestResolution(f"request under reservation {res.id} was abandoned; not sending")
            conn.execute("INSERT INTO provider_request_sends (reservation_id, fence, worker_id) VALUES (%s, %s, %s)"
                         " ON CONFLICT (reservation_id) DO NOTHING", (res.id, ctx.fence, ctx.worker_id))
        self._hook("gateway.after_send_permit", {"reservation_id": res.id, "stage": ctx.stage})

        try:
            resp = self.provider.complete(model=cfg.model, template_id=template_id, prompt=prompt,
                                          max_output_tokens=cfg.max_output_tokens, request_key=request_key)
        except ProviderNotSent as exc:
            state = record_provider_error(conn, res.id, outcome="not_sent", note=f"not sent: {exc}")
            if state != "released":  # a successor or operator owns this hold now
                self._raise_if_fenced_out(ctx, exc)
            raise
        except Exception as exc:  # outcome unknown: keep the hold; the local sender is done
            state = record_provider_error(conn, res.id, outcome="outcome_unknown",
                                          note=f"outcome unknown: {type(exc).__name__}: {exc}")
            self._raise_if_fenced_out(ctx, exc)
            raise BillingUncertain(f"reservation {res.id} retained ({state}): call outcome unknown ({exc})") from exc
        self._hook("gateway.after_send", {"reservation_id": res.id})

        receipt = record_response(conn, self.artifacts, res.id, resp)
        self._hook("gateway.after_settle", {"reservation_id": res.id, "provider_call_id": receipt.provider_call_id})
        return GatewayResult(resp.text, receipt.provider_call_id, False)

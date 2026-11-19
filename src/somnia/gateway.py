"""The single budget-guarded path for model calls.

reserve(upper bound) -> send -> settle(actual). Outcomes:
  * definitively not sent     -> reservation released;
  * unknown (timeout, crash)  -> reservation kept as 'uncertain', BillingUncertain raised;
  * completed                 -> prompt/response stored as artifacts, reservation settled, call recorded.
A completed call with the same request key in the same run is reused instead of re-sent, so a
stage that crashed after a paid call resumes without paying again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import psycopg

from . import budget
from .artifacts import ArtifactStore
from .contracts import GatewayConfig
from .errors import BillingUncertain
from .hashing import hash_obj, sha256_text
from .interfaces import ModelProvider, ProviderNotSent

Hook = Callable[[str, dict], None]


@dataclass(frozen=True)
class CallContext:
    run_id: UUID
    stage: str
    stage_attempt_id: UUID
    envelope_id: UUID
    guard: Callable[[psycopg.Connection], None]


@dataclass(frozen=True)
class GatewayResult:
    text: str
    provider_call_id: UUID
    reused: bool


class ModelGateway:
    def __init__(self, conn: psycopg.Connection, artifacts: ArtifactStore, provider: ModelProvider,
                 cfg: GatewayConfig, hook: Hook | None = None):
        self.conn, self.artifacts, self.provider, self.cfg = conn, artifacts, provider, cfg
        self._hook = hook or (lambda name, info: None)

    def upper_bound_micro(self) -> int:
        c = self.cfg
        return c.max_input_tokens * c.input_micro_per_token + c.max_output_tokens * c.output_micro_per_token

    def call(self, ctx: CallContext, template_id: str, prompt: str) -> GatewayResult:
        conn, cfg = self.conn, self.cfg
        request_key = hash_obj({"run": ctx.run_id, "stage": ctx.stage, "template": template_id,
                                "provider": self.provider.name, "model": cfg.model, "prompt": sha256_text(prompt)})
        prior = conn.execute("SELECT id, response_artifact FROM provider_calls WHERE run_id = %s AND request_key = %s",
                             (ctx.run_id, request_key)).fetchone()
        if prior is not None:
            return GatewayResult(self.artifacts.get(prior["response_artifact"]).decode("utf-8"), prior["id"], True)

        env_unit = conn.execute("SELECT unit FROM budget_envelopes WHERE id = %s", (ctx.envelope_id,)).fetchone()["unit"]
        if not (self.provider.unit == cfg.unit == env_unit):
            raise ValueError(f"unit mismatch: provider {self.provider.unit}, config {cfg.unit}, envelope {env_unit}")
        if self.provider.is_mock != (cfg.unit == "MOCK_MICRO"):
            raise ValueError("mock providers must use MOCK_MICRO and real providers CAD_MICRO")
        in_tokens = self.provider.count_tokens(prompt)
        if in_tokens > cfg.max_input_tokens:
            raise ValueError(f"prompt ({in_tokens} tokens) exceeds the reserved input bound {cfg.max_input_tokens}")

        res = budget.reserve(
            conn, envelope_id=ctx.envelope_id, amount_micro=self.upper_bound_micro(),
            idempotency_key=f"{request_key}:{ctx.stage_attempt_id}", purpose=f"{ctx.stage}:{template_id}",
            provider=self.provider.name, model=cfg.model,
            pricing_snapshot={**cfg.model_dump(mode="json"), "bound": "max_input*in_rate + max_output*out_rate"},
            run_id=ctx.run_id, stage_attempt_id=ctx.stage_attempt_id, guard=ctx.guard)
        if res.state != "reserved":
            raise BillingUncertain(f"reservation {res.id} for this attempt is already {res.state}")
        self._hook("gateway.after_reserve", {"reservation_id": res.id})

        try:
            resp = self.provider.complete(model=cfg.model, template_id=template_id, prompt=prompt,
                                          max_output_tokens=cfg.max_output_tokens, request_key=request_key)
        except ProviderNotSent as exc:
            budget.release_unsent(conn, res.id, note=f"provider rejected before acceptance: {exc}")
            raise
        except Exception as exc:  # outcome unknown: keep the hold
            budget.mark_uncertain(conn, res.id, note=f"{type(exc).__name__}: {exc}")
            raise BillingUncertain(f"reservation {res.id} retained: call outcome unknown ({exc})") from exc
        self._hook("gateway.after_send", {"reservation_id": res.id})

        actual = resp.input_tokens * cfg.input_micro_per_token + resp.output_tokens * cfg.output_micro_per_token
        origin = "mock" if self.provider.is_mock else "observed"
        with conn.transaction():
            p_sha = self.artifacts.put(conn, prompt.encode("utf-8"), media_type="text/plain", origin=origin)
            r_sha = self.artifacts.put(conn, resp.text.encode("utf-8"), media_type="application/json", origin=origin)
            state = conn.execute("SELECT state FROM reservations WHERE id = %s FOR UPDATE", (res.id,)).fetchone()["state"]
            if state == "reserved":
                budget.settle(conn, res.id, actual_micro=actual, request_id=resp.request_id)
            else:  # a successor marked it uncertain after our lease expired; we hold the evidence
                budget.reconcile(conn, res.id, actual_micro=actual, request_id=resp.request_id,
                                 note="settled by original caller with provider-reported usage after lease loss")
            row = conn.execute(
                "INSERT INTO provider_calls (reservation_id, run_id, stage_attempt_id, request_key, provider, model,"
                " is_mock, prompt_artifact, response_artifact, input_tokens, output_tokens, request_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (res.id, ctx.run_id, ctx.stage_attempt_id, request_key, self.provider.name, cfg.model,
                 self.provider.is_mock, p_sha, r_sha, resp.input_tokens, resp.output_tokens, resp.request_id),
            ).fetchone()
        self._hook("gateway.after_settle", {"reservation_id": res.id, "provider_call_id": row["id"]})
        return GatewayResult(resp.text, row["id"], False)

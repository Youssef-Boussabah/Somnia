"""Durable, resumable sleep-cycle engine (mock mode).

Durability model (ARCHITECTURE.md "Job semantics"): at-least-once stage execution with
idempotent, fenced writes — not exactly-once execution.

  * A worker claims a run by incrementing its ``fence`` under an expiring lease (DB clock).
  * Each stage's domain outputs, its attempt record and the stage-pointer move commit in ONE
    transaction that first re-checks ``fence`` and lease liveness. An expired or superseded
    worker therefore cannot commit anything; its partial work is discarded.
  * Side-effecting calls that commit separately (budget reservations, activation) also run the
    lease check inside their own transactions and are idempotent by key.
  * On claim, a new worker marks predecessors' running attempts 'abandoned' and their still-held
    reservations 'uncertain'. Any uncertain reservation blocks the run until reconciled.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .activation import activate_candidate
from .artifacts import ArtifactStore
from .contracts import GateRequest, RunConfig, ScenarioSpec, SkillBody
from .errors import (FixtureError, IdempotencyConflict, IneligibleCandidate, IntegrityFailure, OperatorActionRequired,
                     LeaseLost, LeaseUnavailable, RunBlocked, StaleActivation, StaleParentVersion, StaleValidation,
                     translating)
from .gateway import CallContext, ModelGateway
from .hashing import canonical_json, hash_obj, sha256_text
from .interfaces import GateValidator, ModelProvider, ScenarioExecutor
from .mock import MockExecutor, MockGate, MockProvider, load_mock_script
from .skills import create_skill_version, export_skill_set, load_candidate, record_gate_decision, set_members
from .snapshots import snapshot_events
from .stages import DONE, STAGES, next_stage

MOCK_SUITE_ID = "mock-suite-v1"
Hook = Callable[[str, dict], None]


class SimulatedCrash(BaseException):
    """Test-only: models abrupt process death. Derives from BaseException so no cleanup runs."""


# ---------------------------------------------------------------- run lifecycle
def create_run(conn: psycopg.Connection, *, project_id: UUID, snapshot_id: UUID, envelope_id: UUID,
               config: RunConfig, idempotency_key: str) -> UUID:
    """Persist a pending run. Idempotent by key; the key cannot be reused for different inputs."""
    script = load_mock_script(config.mock_script)
    record = {"config": config.model_dump(mode="json"), "mock_script_sha256": hash_obj(script)}
    config_hash = hash_obj(record)
    with translating(), conn.transaction():
        snap = conn.execute("SELECT project_id FROM snapshots WHERE id = %s", (snapshot_id,)).fetchone()
        env = conn.execute("SELECT project_id, unit FROM budget_envelopes WHERE id = %s", (envelope_id,)).fetchone()
        if snap is None or env is None or snap["project_id"] != project_id or env["project_id"] != project_id:
            raise LookupError("snapshot and envelope must exist and belong to the project")
        if env["unit"] != config.gateway.unit:
            raise ValueError(f"envelope unit {env['unit']} does not match gateway unit {config.gateway.unit}")
        row = conn.execute(
            "INSERT INTO sleep_runs (project_id, snapshot_id, envelope_id, config_schema_version, config, config_hash,"
            " idempotency_key, status, current_stage) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)"
            " ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            (project_id, snapshot_id, envelope_id, config.schema_version, Jsonb(record), config_hash,
             idempotency_key, STAGES[0]),
        ).fetchone()
        if row is not None:
            return row["id"]
        ex = conn.execute("SELECT id, snapshot_id, config_hash, envelope_id FROM sleep_runs WHERE idempotency_key = %s",
                          (idempotency_key,)).fetchone()
        if (ex["snapshot_id"], ex["config_hash"], ex["envelope_id"]) != (snapshot_id, config_hash, envelope_id):
            raise IdempotencyConflict(f"run key {idempotency_key!r} already used for different inputs")
        return ex["id"]


def unblock_run(conn: psycopg.Connection, run_id: UUID) -> None:
    """Operator action after reconciling billing: return a blocked run to 'pending'."""
    with conn.transaction():
        run = conn.execute("SELECT status FROM sleep_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"unknown run {run_id}")
        if run["status"] != "blocked":
            return
        n = conn.execute("SELECT count(*) AS n FROM reservations WHERE run_id = %s AND state = 'uncertain'",
                         (run_id,)).fetchone()["n"]
        if n:
            raise RunBlocked(f"{n} uncertain reservation(s) must be reconciled first")
        conn.execute("UPDATE sleep_runs SET status = 'pending', blocked_reason = NULL WHERE id = %s", (run_id,))


@dataclass
class WorkOutcome:
    run_id: UUID
    status: str
    stages_run: list[str] = field(default_factory=list)
    blocked_reason: str | None = None


@dataclass
class _Ctx:
    run: dict[str, Any]
    config: RunConfig
    script: dict[str, Any]
    fence: int
    attempt_id: UUID
    stage: str
    base_set_id: UUID


@dataclass(frozen=True)
class _Commit:
    """A stage's commit: ``write`` runs before the attempt/pointer update; ``after_advance`` (optional)
    runs after it, inside the same fenced transaction, and its result is merged into the output."""
    write: Callable[[psycopg.Connection], dict]
    after_advance: Callable[[psycopg.Connection], dict] | None = None


class SleepWorker:
    def __init__(self, conn: psycopg.Connection, artifacts: ArtifactStore, *, worker_id: str,
                 lease_ttl_s: float = 30.0, provider: ModelProvider | None = None,
                 gate: GateValidator | None = None, executor: ScenarioExecutor | None = None,
                 hook: Hook | None = None):
        self.conn, self.artifacts, self.worker_id, self.ttl = conn, artifacts, worker_id, lease_ttl_s
        self._provider, self._gate, self._executor = provider, gate, executor
        self._hook_fn = hook

    def _hook(self, name: str, **info: Any) -> None:
        if self._hook_fn is not None:
            self._hook_fn(name, {"worker_id": self.worker_id, **info})

    # ------------------------------------------------------------ leases
    def _assert_lease(self, conn: psycopg.Connection, run_id: UUID, fence: int, lock: str = "SHARE") -> dict:
        row = conn.execute(
            f"SELECT fence, current_stage, lease_expires_at > clock_timestamp() AS live FROM sleep_runs"
            f" WHERE id = %s FOR {'UPDATE' if lock == 'UPDATE' else 'SHARE'}", (run_id,)).fetchone()
        if row is None or row["fence"] != fence or not row["live"]:
            raise LeaseLost(f"worker {self.worker_id} (fence {fence}) no longer holds run {run_id}")
        return row

    def _claim(self, run_id: UUID) -> int:
        with conn_tx(self.conn):
            row = self.conn.execute(
                "UPDATE sleep_runs SET fence = fence + 1, lease_owner = %s,"
                " lease_expires_at = clock_timestamp() + make_interval(secs => %s), status = 'running'"
                " WHERE id = %s AND status IN ('pending', 'running')"
                " AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp()) RETURNING fence",
                (self.worker_id, self.ttl, run_id)).fetchone()
            if row is None:
                st = self.conn.execute("SELECT status, blocked_reason, lease_owner FROM sleep_runs WHERE id = %s",
                                       (run_id,)).fetchone()
                if st is None:
                    raise LookupError(f"unknown run {run_id}")
                if st["status"] == "blocked":
                    raise RunBlocked(st["blocked_reason"])
                if st["status"] in ("completed", "failed"):
                    raise LeaseUnavailable(f"run is {st['status']}")
                raise LeaseUnavailable(f"run leased by {st['lease_owner']}")
            fence = row["fence"]
            self.conn.execute(
                "UPDATE stage_attempts SET status = 'abandoned', finished_at = clock_timestamp(),"
                " error = 'lease expired or superseded' WHERE run_id = %s AND status = 'running' AND fence < %s",
                (run_id, fence))
            orphans = self.conn.execute(
                "SELECT id FROM reservations WHERE run_id = %s AND state = 'reserved'", (run_id,)).fetchall()
            for o in orphans:  # a predecessor may have sent these calls: never assume they were free
                self.conn.execute("UPDATE reservations SET state = 'uncertain', resolution_note = %s WHERE id = %s",
                                  (f"held by an abandoned attempt; marked uncertain by {self.worker_id}", o["id"]))
            n_unc = self.conn.execute("SELECT count(*) AS n FROM reservations WHERE run_id = %s AND state = 'uncertain'",
                                      (run_id,)).fetchone()["n"]
            if n_unc:
                reason = f"uncertain_billing: {n_unc} reservation(s) need reconciliation before replay"
                self.conn.execute("UPDATE sleep_runs SET status = 'blocked', blocked_reason = %s, lease_owner = NULL,"
                                  " lease_expires_at = NULL WHERE id = %s", (reason, run_id))
                blocked = reason
            else:
                blocked = None
        if blocked:
            raise RunBlocked(blocked)
        return fence

    def _heartbeat(self, run_id: UUID, fence: int) -> None:
        row = self.conn.execute(
            "UPDATE sleep_runs SET lease_expires_at = clock_timestamp() + make_interval(secs => %s)"
            " WHERE id = %s AND fence = %s AND lease_expires_at > clock_timestamp() RETURNING id",
            (self.ttl, run_id, fence)).fetchone()
        if row is None:
            raise LeaseLost(f"heartbeat failed for worker {self.worker_id} (fence {fence})")

    def _release(self, run_id: UUID, fence: int) -> None:
        self.conn.execute("UPDATE sleep_runs SET lease_owner = NULL, lease_expires_at = NULL"
                          " WHERE id = %s AND fence = %s", (run_id, fence))

    # ------------------------------------------------------------ main loop
    def work(self, run_id: UUID, *, max_stages: int | None = None) -> WorkOutcome:
        run = self.conn.execute("SELECT * FROM sleep_runs WHERE id = %s", (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"unknown run {run_id}")
        if run["status"] == "completed":
            return WorkOutcome(run_id, "completed")
        config = RunConfig.model_validate(run["config"]["config"])
        script = load_mock_script(config.mock_script)
        if hash_obj(script) != run["config"]["mock_script_sha256"]:
            raise IntegrityFailure("mock script changed since the run was created; create a new run")
        try:
            fence = self._claim(run_id)
        except RunBlocked as exc:
            return WorkOutcome(run_id, "blocked", blocked_reason=str(exc))
        base_set = self.conn.execute("SELECT base_skill_set_id FROM snapshots WHERE id = %s",
                                     (run["snapshot_id"],)).fetchone()["base_skill_set_id"]
        outcome = WorkOutcome(run_id, "running")
        provider = self._provider or MockProvider(script)
        gateway = ModelGateway(self.conn, self.artifacts, provider, config.gateway, hook=self._hook_fn)
        attempt_id: UUID | None = None
        try:
            while max_stages is None or len(outcome.stages_run) < max_stages:
                self._heartbeat(run_id, fence)
                stage = self.conn.execute("SELECT current_stage FROM sleep_runs WHERE id = %s",
                                          (run_id,)).fetchone()["current_stage"]
                if stage == DONE:
                    break
                attempt_id = self._begin_attempt(run, stage, fence)
                ctx = _Ctx(run, config, script, fence, attempt_id, stage, base_set)
                commit_fn = getattr(self, f"_stage_{stage}")(ctx, gateway)
                self._hook("stage.before_commit", stage=stage, fence=fence)
                self._commit_stage(ctx, commit_fn)
                self._hook("stage.after_commit", stage=stage, fence=fence)
                outcome.stages_run.append(stage)
                attempt_id = None
        except OperatorActionRequired as exc:  # uncertain billing, outstanding request, frozen budget
            reason = f"{exc.reason_code}: {exc}"
            self._block(run_id, fence, attempt_id, reason)
            outcome.status, outcome.blocked_reason = "blocked", reason
            return outcome
        except LeaseLost:
            raise
        except Exception as exc:
            self._fail_attempt(run_id, fence, attempt_id, exc)
            raise
        final = self.conn.execute("SELECT status FROM sleep_runs WHERE id = %s", (run_id,)).fetchone()["status"]
        self._release(run_id, fence)
        outcome.status = final
        return outcome

    def _begin_attempt(self, run: dict, stage: str, fence: int) -> UUID:
        prior = self.conn.execute(
            "SELECT stage, output_hash FROM stage_attempts WHERE run_id = %s AND status = 'succeeded'",
            (run["id"],)).fetchall()
        prior_hashes = sorted((p["stage"], p["output_hash"]) for p in prior)
        input_hash = hash_obj({"stage": stage, "config_hash": run["config_hash"], "snapshot_id": run["snapshot_id"],
                               "prior": prior_hashes})
        with conn_tx(self.conn):
            self._assert_lease(self.conn, run["id"], fence)
            return self.conn.execute(
                "INSERT INTO stage_attempts (run_id, stage, fence, worker_id, status, input_hash)"
                " VALUES (%s, %s, %s, %s, 'running', %s) RETURNING id",
                (run["id"], stage, fence, self.worker_id, input_hash)).fetchone()["id"]

    def _commit_stage(self, ctx: _Ctx, commit: Callable[[psycopg.Connection], dict] | _Commit) -> None:
        c = commit if isinstance(commit, _Commit) else _Commit(commit)
        run_id = ctx.run["id"]
        with conn_tx(self.conn):
            row = self._assert_lease(self.conn, run_id, ctx.fence, lock="UPDATE")
            if row["current_stage"] != ctx.stage:
                raise LeaseLost(f"stage pointer moved to {row['current_stage']} while running {ctx.stage}")
            output = c.write(self.conn)
            self.conn.execute(
                "UPDATE stage_attempts SET status = 'succeeded', output = %s, output_hash = %s,"
                " finished_at = clock_timestamp() WHERE id = %s",
                (Jsonb(output) if c.after_advance is None else None,
                 hash_obj(output) if c.after_advance is None else None, ctx.attempt_id))
            nxt = next_stage(ctx.stage)
            moved = self.conn.execute(
                "UPDATE sleep_runs SET current_stage = %s, status = %s,"
                " finished_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END WHERE id = %s AND fence = %s",
                (nxt, "completed" if nxt == DONE else "running", nxt == DONE, run_id, ctx.fence)).rowcount
            if moved != 1:  # defence in depth; _assert_lease above should already have failed
                raise LeaseLost(f"stage pointer update matched {moved} rows for fence {ctx.fence}")
            if c.after_advance is not None:
                # Still inside the same fenced transaction: the step sees the run as it will be once
                # this commits (attempt succeeded, pointer moved). A crash here commits nothing.
                output = {**output, **c.after_advance(self.conn)}
                self.conn.execute("UPDATE stage_attempts SET output = %s, output_hash = %s WHERE id = %s",
                                  (Jsonb(output), hash_obj(output), ctx.attempt_id))

    def _block(self, run_id: UUID, fence: int, attempt_id: UUID | None, reason: str) -> None:
        with conn_tx(self.conn):
            if attempt_id is not None:
                self.conn.execute("UPDATE stage_attempts SET status = 'failed', error = %s,"
                                  " finished_at = clock_timestamp() WHERE id = %s AND status = 'running'",
                                  (reason, attempt_id))
            self.conn.execute("UPDATE sleep_runs SET status = 'blocked', blocked_reason = %s, lease_owner = NULL,"
                              " lease_expires_at = NULL WHERE id = %s AND fence = %s", (reason, run_id, fence))

    def _fail_attempt(self, run_id: UUID, fence: int, attempt_id: UUID | None, exc: Exception) -> None:
        try:
            with conn_tx(self.conn):
                if attempt_id is not None:
                    self.conn.execute("UPDATE stage_attempts SET status = 'failed', error = %s,"
                                      " finished_at = clock_timestamp() WHERE id = %s AND status = 'running'",
                                      (f"{type(exc).__name__}: {exc}", attempt_id))
                self._release(run_id, fence)
        except psycopg.Error:
            pass  # the original exception is more informative

    def _guard(self, ctx: _Ctx) -> Callable[[psycopg.Connection], None]:
        return lambda c: self._assert_lease(c, ctx.run["id"], ctx.fence)

    def _call_ctx(self, ctx: _Ctx) -> CallContext:
        return CallContext(run_id=ctx.run["id"], stage=ctx.stage, stage_attempt_id=ctx.attempt_id,
                           envelope_id=ctx.run["envelope_id"], guard=self._guard(ctx), fence=ctx.fence,
                           worker_id=self.worker_id)

    # ------------------------------------------------------------ stages
    def _stage_consolidate(self, ctx: _Ctx, gw: ModelGateway):
        # consolidate.v2: events are cited by snapshot-local refs ("s<session>/<key>"), because
        # idempotency keys repeat across sessions. v1 cited bare keys and could misattribute evidence.
        events = snapshot_events(self.conn, ctx.run["snapshot_id"])
        by_ref = {e["ref"]: e["id"] for e in events}
        if len(by_ref) != len(events):  # cannot happen given UNIQUE (session_id, idempotency_key)
            raise IntegrityFailure("snapshot event references are not unique")
        prompt = "TEMPLATE consolidate.v2\n" + canonical_json(
            [{"ref": e["ref"], "session": e["session_ordinal"], "seq": e["seq"], "type": e["event_type"],
              "payload": e["payload"]} for e in events])
        res = gw.call(self._call_ctx(ctx), "consolidate.v2", prompt)
        hyps_all = json.loads(res.text)["hypotheses"]
        hyps = hyps_all[: ctx.config.limits.max_hypotheses]
        for h in hyps:
            if "source_event_keys" in h:
                raise FixtureError("hypothesis cites bare event keys (consolidate.v1); cite source_event_refs")
            if not all(h.get(k) for k in ("claim", "mechanism", "falsifier", "source_event_refs")):
                raise FixtureError("hypothesis lacks claim/mechanism/falsifier/source_event_refs")
            unknown = [k for k in h["source_event_refs"] if k not in by_ref]
            if unknown:
                raise FixtureError(f"hypothesis cites event refs that are not in the snapshot: {unknown}")

        def commit(conn: psycopg.Connection) -> dict:
            for i, h in enumerate(hyps, start=1):
                hid = conn.execute(
                    "INSERT INTO hypotheses (run_id, stage_attempt_id, ordinal, claim, mechanism, falsifier,"
                    " provider_call_id, origin, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, 'mock', %s)"
                    " RETURNING id", (ctx.run["id"], ctx.attempt_id, i, h["claim"], h["mechanism"], h["falsifier"],
                                      res.provider_call_id, hash_obj(h))).fetchone()["id"]
                for event_id in {by_ref[k] for k in h["source_event_refs"]}:
                    conn.execute("INSERT INTO hypothesis_sources (hypothesis_id, event_id) VALUES (%s, %s)",
                                 (hid, event_id))
            return {"hypotheses": len(hyps), "truncated": len(hyps_all) - len(hyps),
                    "provider_call_id": str(res.provider_call_id), "reused_call": res.reused}
        return commit

    def _stage_scenarios(self, ctx: _Ctx, gw: ModelGateway):
        hyps = self.conn.execute("SELECT id, ordinal FROM hypotheses WHERE run_id = %s ORDER BY ordinal",
                                 (ctx.run["id"],)).fetchall()
        planned = []
        for h in hyps:
            for s in ctx.script.get("scenarios", {}).get(str(h["ordinal"]), [])[
                    : ctx.config.limits.max_scenarios_per_hypothesis]:
                spec = ScenarioSpec(title=s["title"], intervention=s["intervention"],
                                    expected_invariant=s["expected_invariant"],
                                    reference_hash=sha256_text(s["reference_impl"]),
                                    faulty_hash=sha256_text(s["faulty_impl"]), oracle_hash=sha256_text(s["oracle"]))
                checks = s.get("mock_validity_checks", {})
                required = ("reference_passes", "known_fault_detected", "intervention_reachable")
                if any(k not in checks for k in required):
                    validity, reason = "inconclusive", "validity checks incomplete"
                else:
                    failed = [k for k in required if checks[k] is not True]
                    validity = "valid" if not failed else "invalid"
                    reason = "all mock validity checks passed" if not failed else f"failed: {', '.join(failed)}"
                planned.append((h["id"], spec, checks, validity, f"MOCK: {reason}"))

        def commit(conn: psycopg.Connection) -> dict:
            counts = {"valid": 0, "invalid": 0, "inconclusive": 0}
            for i, (hid, spec, checks, validity, reason) in enumerate(planned, start=1):
                body = {**spec.model_dump(mode="json"), "mock_validity_checks": checks}
                conn.execute(
                    "INSERT INTO scenarios (run_id, hypothesis_id, ordinal, spec, spec_hash, validity, validity_reason,"
                    " origin) VALUES (%s, %s, %s, %s, %s, %s, %s, 'mock')",
                    (ctx.run["id"], hid, i, Jsonb(body), hash_obj(body), validity, reason))
                counts[validity] += 1
            return counts
        return commit

    def _stage_rehearse(self, ctx: _Ctx, gw: ModelGateway):
        executor = self._executor or MockExecutor(ctx.script)
        rows = self.conn.execute("SELECT id, ordinal, spec FROM scenarios WHERE run_id = %s AND validity = 'valid'"
                                 " ORDER BY ordinal LIMIT %s",
                                 (ctx.run["id"], ctx.config.limits.max_executed_scenarios)).fetchall()
        results = []
        for r in rows:
            spec = ScenarioSpec.model_validate({k: v for k, v in r["spec"].items() if k != "mock_validity_checks"})
            scripted = len(ctx.script.get("rehearsal_outcomes", {}).get(str(r["ordinal"]), [])) or 1
            for attempt in range(1, min(scripted, ctx.config.limits.max_attempts_per_scenario) + 1):
                results.append((r["id"], attempt, executor.execute(spec, scenario_key=str(r["ordinal"]),
                                                                   attempt_no=attempt)))

        def commit(conn: psycopg.Connection) -> dict:
            tally: dict[str, int] = {}
            for sid, attempt, res in results:
                conn.execute(
                    "INSERT INTO experiment_runs (run_id, scenario_id, attempt_no, executor, is_mock, outcome,"
                    " solution_hash, diagnostics) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (ctx.run["id"], sid, attempt, res.executor, res.is_mock, res.outcome, res.solution_hash,
                     Jsonb(res.diagnostics)))
                tally[res.outcome] = tally.get(res.outcome, 0) + 1
            return {"executed_attempts": len(results), "outcomes": tally}
        return commit

    def _stage_extract(self, ctx: _Ctx, gw: ModelGateway):
        run_id = ctx.run["id"]
        hyps = self.conn.execute("SELECT id, ordinal, claim, falsifier FROM hypotheses WHERE run_id = %s"
                                 " ORDER BY ordinal", (run_id,)).fetchall()
        exps = self.conn.execute(
            "SELECT x.id, s.ordinal AS scenario_ordinal, x.attempt_no, x.outcome FROM experiment_runs x"
            " JOIN scenarios s ON s.id = x.scenario_id WHERE x.run_id = %s ORDER BY s.ordinal, x.attempt_no",
            (run_id,)).fetchall()
        prompt = "TEMPLATE extract.v1\n" + canonical_json({
            "hypotheses": [{"ordinal": h["ordinal"], "claim": h["claim"], "falsifier": h["falsifier"]} for h in hyps],
            "experiments": [{"scenario": e["scenario_ordinal"], "attempt": e["attempt_no"], "outcome": e["outcome"]}
                            for e in exps]})
        res = gw.call(self._call_ctx(ctx), "extract.v1", prompt)
        cands = json.loads(res.text)["candidates"][: ctx.config.limits.max_candidates]
        hyp_by_ord = {h["ordinal"]: h["id"] for h in hyps}
        exps_by_scn: dict[int, list[UUID]] = {}
        for e in exps:
            exps_by_scn.setdefault(e["scenario_ordinal"], []).append(e["id"])
        base_members = {}
        for m in set_members(self.conn, ctx.base_set_id):
            slug = self.conn.execute("SELECT slug FROM skills WHERE id = %s", (m["skill_id"],)).fetchone()["slug"]
            base_members[slug] = m["skill_version_id"]
        project_id = ctx.run["project_id"]

        def commit(conn: psycopg.Connection) -> dict:
            out = []
            for c in cands:
                body = SkillBody(**{k: c[k] for k in ("slug", "title", "procedure", "applicability", "exclusions",
                                                      "prerequisites", "known_limits")})
                parent = base_members.get(body.slug)
                sv = create_skill_version(conn, project_id=project_id, body=body, parent_version_id=parent,
                                          origin="mock", run_id=run_id)
                ev = c.get("evidence", {})
                hyp_ids = [hyp_by_ord[o] for o in ev.get("hypothesis_ordinals", [])]
                exp_ids = [x for o in ev.get("scenario_ordinals", []) for x in exps_by_scn.get(o, [])]
                if not hyp_ids or not exp_ids:
                    raise FixtureError(f"candidate {body.slug} must cite hypotheses and executed experiments")
                src_events = conn.execute("SELECT DISTINCT event_id FROM hypothesis_sources WHERE hypothesis_id = ANY(%s)"
                                          " ORDER BY event_id", (hyp_ids,)).fetchall()
                rows = ([("hypothesis_id", h) for h in hyp_ids] + [("experiment_run_id", x) for x in exp_ids]
                        + [("event_id", e["event_id"]) for e in src_events]
                        + [("provider_call_id", res.provider_call_id)])
                for col, val in rows:
                    conn.execute(f"INSERT INTO skill_version_evidence (skill_version_id, role, {col})"
                                 f" VALUES (%s, 'supporting', %s)", (sv["skill_version_id"], val))
                out.append({"slug": body.slug, "skill_version_id": str(sv["skill_version_id"]),
                            "version": sv["version"], "parent_version_id": str(parent) if parent else None,
                            "content_hash": sv["content_hash"]})
            return {"candidates": out, "provider_call_id": str(res.provider_call_id), "reused_call": res.reused}
        return commit

    def _stage_gate(self, ctx: _Ctx, gw: ModelGateway):
        gate = self._gate or MockGate(ctx.script)
        rows = self.conn.execute(
            "SELECT v.id FROM skill_versions v JOIN skills s ON s.id = v.skill_id WHERE v.created_by_run_id = %s"
            " ORDER BY s.slug, v.version", (ctx.run["id"],)).fetchall()
        baseline = export_skill_set(self.conn, ctx.base_set_id)
        verdicts = []
        for r in rows:
            cand = load_candidate(self.conn, r["id"])
            verdict = gate.validate(GateRequest(candidate=cand, baseline_skill_set=baseline, suite_id=MOCK_SUITE_ID))
            if gate.is_mock and (verdict.independent or not verdict.is_mock):
                raise IntegrityFailure("a mock gate returned a verdict claiming independence or non-mock status")
            verdicts.append((cand, verdict))

        def commit(conn: psycopg.Connection) -> dict:
            out = []
            for cand, verdict in verdicts:
                gid = record_gate_decision(conn, skill_version_id=cand.skill_version_id,
                                           baseline_skill_set_id=ctx.base_set_id, verdict=verdict, run_id=ctx.run["id"])
                out.append({"slug": cand.body.slug, "skill_version_id": str(cand.skill_version_id),
                            "gate_decision_id": str(gid), "decision": verdict.decision, "is_mock": verdict.is_mock,
                            "independent": verdict.independent})
            return {"decisions": out, "validator": gate.name, "validator_is_mock": gate.is_mock}
        return commit

    def _stage_activate(self, ctx: _Ctx, gw: ModelGateway):
        run_id = ctx.run["id"]
        outcomes = []
        if ctx.config.activation.auto_activate:
            passing = self.conn.execute(
                "SELECT g.id, s.slug FROM gate_decisions g JOIN skill_versions v ON v.id = g.skill_version_id"
                " JOIN skills s ON s.id = v.skill_id WHERE g.run_id = %s AND g.decision = 'pass'"
                " ORDER BY s.slug, v.version", (run_id,)).fetchall()
            # Every verdict of this run was produced against the run's base set, so each activation
            # must find exactly that set active. Once one candidate is activated, the others' verdicts
            # are stale: they are recorded as such and need fresh validation (never chained).
            for g in passing:
                rec = {"slug": g["slug"], "gate_decision_id": str(g["id"])}
                try:
                    r = activate_candidate(
                        self.conn, project_id=ctx.run["project_id"], gate_decision_id=g["id"],
                        expected_previous_set_id=ctx.base_set_id, actor=f"sleep_run:{run_id}",
                        idempotency_key=f"run:{run_id}:gate:{g['id']}",
                        reason="auto-activation in controlled development condition (mock gate)",
                        allow_mock_validation=ctx.config.gate.allow_mock_gate_activation, run_id=run_id,
                        guard=self._guard(ctx))
                    rec |= {"result": "activated" if r.applied else "already_activated",
                            "activation_id": str(r.activation_id), "new_set_id": str(r.new_set_id),
                            "new_set_version": r.new_set_version}
                except StaleValidation as exc:
                    rec |= {"result": "stale_validation", "requires": "fresh validation against the active set",
                            "detail": f"{type(exc).__name__}: {exc}"}
                except (StaleActivation, StaleParentVersion) as exc:
                    rec |= {"result": "stale_rejected", "detail": f"{type(exc).__name__}: {exc}"}
                except IneligibleCandidate as exc:
                    rec |= {"result": "ineligible", "detail": str(exc)}
                outcomes.append(rec)
                self._hook("activation.after_attempt", record=rec)

        def commit(conn: psycopg.Connection) -> dict:
            after = conn.execute("SELECT skill_set_id FROM active_skill_sets WHERE project_id = %s",
                                 (ctx.run["project_id"],)).fetchone()["skill_set_id"]
            return {"auto_activate": ctx.config.activation.auto_activate, "outcomes": outcomes,
                    "active_set_before_run": str(ctx.base_set_id), "active_set_at_commit": str(after)}
        return commit

    def _stage_report(self, ctx: _Ctx, gw: ModelGateway):
        from .report import build_report

        def archive(conn: psycopg.Connection) -> dict:
            # Runs after the attempt is marked succeeded and the run completed, in the same
            # transaction, so the archived bytes describe the final state. A report cannot contain
            # its own artifact hash: inside the archive, the report attempt's output_hash is null.
            report = build_report(conn, ctx.run["id"])
            sha = self.artifacts.put(conn, canonical_json(report).encode("utf-8"), media_type="application/json",
                                     origin="derived")
            return {"report_artifact": sha}
        return _Commit(write=lambda conn: {}, after_advance=archive)


@contextmanager
def conn_tx(conn: psycopg.Connection) -> Iterator[None]:
    """A transaction whose Somnia SQLSTATEs (including at commit) surface as domain errors."""
    with translating(), conn.transaction():
        yield

"""Run reports with evidence references, and provenance tracing for skill versions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from . import budget
from .artifacts import ArtifactStore
from .errors import IntegrityFailure
from .events import verify_session_chain
from .hashing import hash_obj
from .skills import load_candidate
from .stages import STAGES

REPORT_SCHEMA_VERSION = 2  # 2: budget_alerts; unresolved.outstanding_requests / billing_discrepancies (D044)
MOCK_LABELS = [
    "MOCK MODEL RESPONSES (scripted fixture text, no model was called)",
    "SYNTHETIC DEVELOPMENT FIXTURES (fake experience and outcomes)",
    "MOCK GATE (not independent validation; paired scores are synthetic)",
    "MOCK EXECUTION (no generated code was run)",
    "NO RESEARCH RESULT: this run demonstrates infrastructure only",
]


def _s(v: Any) -> Any:
    return str(v) if isinstance(v, UUID) else v


def build_report(conn: psycopg.Connection, run_id: UUID) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM sleep_runs WHERE id = %s", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"unknown run {run_id}")
    snap = conn.execute("SELECT * FROM snapshots WHERE id = %s", (run["snapshot_id"],)).fetchone()
    attempts = conn.execute(
        "SELECT stage, fence, worker_id, status, output_hash, error, started_at, finished_at FROM stage_attempts"
        " WHERE run_id = %s ORDER BY started_at", (run_id,)).fetchall()
    stages = [{"stage": st, "attempts": [
        {"fence": a["fence"], "worker_id": a["worker_id"], "status": a["status"], "output_hash": a["output_hash"],
         "error": a["error"]} for a in attempts if a["stage"] == st]} for st in STAGES]

    hyps = conn.execute("SELECT id, ordinal, claim, falsifier, provider_call_id, origin FROM hypotheses"
                        " WHERE run_id = %s ORDER BY ordinal", (run_id,)).fetchall()
    hyp_out = []
    for h in hyps:
        src = conn.execute("SELECT event_id FROM hypothesis_sources WHERE hypothesis_id = %s ORDER BY event_id",
                           (h["id"],)).fetchall()
        hyp_out.append({"id": str(h["id"]), "ordinal": h["ordinal"], "claim": h["claim"], "falsifier": h["falsifier"],
                        "origin": h["origin"], "provider_call_id": _s(h["provider_call_id"]),
                        "source_event_ids": [str(s["event_id"]) for s in src]})
    scen = conn.execute(
        "SELECT s.id, s.ordinal, h.ordinal AS hypothesis, s.spec->>'title' AS title, s.validity, s.validity_reason"
        " FROM scenarios s JOIN hypotheses h ON h.id = s.hypothesis_id WHERE s.run_id = %s ORDER BY s.ordinal",
        (run_id,)).fetchall()
    exps = conn.execute(
        "SELECT x.id, s.ordinal AS scenario, x.attempt_no, x.outcome, x.executor, x.is_mock FROM experiment_runs x"
        " JOIN scenarios s ON s.id = x.scenario_id WHERE x.run_id = %s ORDER BY s.ordinal, x.attempt_no",
        (run_id,)).fetchall()

    cands = conn.execute(
        "SELECT v.id, s.slug, v.version, v.parent_version_id, v.content_hash, v.token_length, v.origin"
        " FROM skill_versions v JOIN skills s ON s.id = v.skill_id WHERE v.created_by_run_id = %s"
        " ORDER BY s.slug, v.version", (run_id,)).fetchall()
    cand_out = []
    for c in cands:
        ev = conn.execute("SELECT role, event_id, hypothesis_id, experiment_run_id, provider_call_id"
                          " FROM skill_version_evidence WHERE skill_version_id = %s ORDER BY id", (c["id"],)).fetchall()
        gates = conn.execute("SELECT id, decision, validator, validator_version, is_mock, independent, reason"
                             " FROM gate_decisions WHERE skill_version_id = %s ORDER BY created_at", (c["id"],)).fetchall()
        cand_out.append({
            "skill_version_id": str(c["id"]), "slug": c["slug"], "version": c["version"],
            "parent_version_id": _s(c["parent_version_id"]), "content_hash": c["content_hash"],
            "token_length": c["token_length"], "origin": c["origin"],
            "evidence": [f"{k.removesuffix('_id')}:{v}" for e in ev for k, v in e.items() if k != "role" and v],
            "gate_decisions": [{**{k: _s(g[k]) for k in g}} for g in gates]})

    acts = conn.execute("SELECT id, kind, gate_decision_id, previous_set_id, new_set_id, created_at FROM activations"
                        " WHERE run_id = %s ORDER BY created_at", (run_id,)).fetchall()
    act_stage = conn.execute("SELECT output FROM stage_attempts WHERE run_id = %s AND stage = 'activate'"
                             " AND status = 'succeeded'", (run_id,)).fetchone()
    active_now = conn.execute("SELECT a.skill_set_id, s.version, s.contains_mock_validation FROM active_skill_sets a"
                              " JOIN skill_sets s ON s.id = a.skill_set_id WHERE a.project_id = %s",
                              (run["project_id"],)).fetchone()
    base = conn.execute("SELECT id, version FROM skill_sets WHERE id = %s", (snap["base_skill_set_id"],)).fetchone()

    res = conn.execute(
        "SELECT r.id, r.state, r.amount_micro, r.actual_micro, r.purpose, r.request_id, e.unit FROM reservations r"
        " JOIN budget_envelopes e ON e.id = r.envelope_id WHERE r.run_id = %s ORDER BY r.created_at",
        (run_id,)).fetchall()
    calls = conn.execute("SELECT c.id, c.provider, c.model, c.is_mock, c.input_tokens, c.output_tokens, c.request_id,"
                         " c.prompt_artifact, c.response_artifact, o.actor AS operator_recorded_by"
                         " FROM provider_calls c LEFT JOIN operator_recorded_responses o ON o.provider_call_id = c.id"
                         " WHERE c.run_id = %s ORDER BY c.created_at", (run_id,)).fetchall()
    unresolved = [{"reservation_id": str(r["id"]), "state": r["state"], "amount_micro": r["amount_micro"],
                   "unit": r["unit"]} for r in res if r["state"] in ("reserved", "uncertain")]
    failed = [a for a in attempts if a["status"] in ("failed", "abandoned")]
    env = budget.envelope_status(conn, run["envelope_id"])

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": str(run_id), "project_id": str(run["project_id"]), "mode": run["config"]["config"]["mode"],
        "labels": MOCK_LABELS if run["config"]["config"]["mode"] == "mock" else [],
        "status": run["status"], "current_stage": run["current_stage"], "blocked_reason": run["blocked_reason"],
        "config_hash": run["config_hash"], "mock_script_sha256": run["config"].get("mock_script_sha256"),
        "snapshot": {"id": str(snap["id"]), "content_hash": snap["content_hash"], "event_count": snap["event_count"],
                     "base_skill_set": {"id": str(base["id"]), "version": base["version"]}},
        "stages": stages,
        "hypotheses": hyp_out,
        "scenarios": [{k: _s(v) for k, v in s.items()} for s in scen],
        "experiments": [{k: _s(v) for k, v in x.items()} for x in exps],
        "candidates": cand_out,
        "activation": {
            "outcomes": (act_stage["output"]["outcomes"] if act_stage else None),
            "activations": [{k: _s(v) if k != "created_at" else v.isoformat() for k, v in a.items()} for a in acts],
            "active_set_before_run": {"id": str(base["id"]), "version": base["version"]},
            "active_set_now": {"id": str(active_now["skill_set_id"]), "version": active_now["version"],
                               "contains_mock_validation": active_now["contains_mock_validation"]},
        },
        "usage": {
            "envelope": {"name": env["name"], "unit": env["unit"], "limit_micro": env["limit_micro"],
                         "held_micro": env["reserved_micro"], "confirmed_micro": env["confirmed_micro"]},
            "reservations": [{k: _s(v) for k, v in r.items()} for r in res],
            "provider_calls": [{k: _s(v) for k, v in c.items()} for c in calls],
            "unit_note": "MOCK_MICRO units are not money" if env["unit"] == "MOCK_MICRO" else "CAD micro-units",
        },
        "real_spend": budget.spend_summary(conn, run["project_id"])["CAD_MICRO"],
        # Project-wide: overruns (evidence is never trimmed to fit a cap), open billing discrepancies
        # (spending frozen) and outstanding requests (no resend). Empty when nothing needs an operator.
        "budget_alerts": budget.alerts(conn, run["project_id"]),
        "unresolved": {"reservations": unresolved,
                       "outstanding_requests": [{"reservation_id": str(q["reservation_id"]), "hold_state": q["hold_state"]}
                                                for q in budget.outstanding_requests(conn, run["project_id"])
                                                if q["run_id"] == run_id],
                       "billing_discrepancies": [{k: _s(v) for k, v in d.items() if k != "created_at"}
                                                 for d in conn.execute(
                           "SELECT d.id, d.status, d.operator_micro, d.reported_micro, d.delta_micro FROM"
                           " billing_discrepancies d JOIN reservations r ON r.id = d.reservation_id"
                           " WHERE r.run_id = %s ORDER BY d.created_at", (run_id,)).fetchall()],
                       "failed_or_abandoned_attempts": [{"stage": a["stage"], "fence": a["fence"],
                                                         "status": a["status"], "error": a["error"]} for a in failed]},
    }


def render_text(r: dict[str, Any]) -> str:
    out = [f"Somnia sleep-run report  run={r['run_id']}", f"status={r['status']}  stage={r['current_stage']}"
           + (f"  BLOCKED: {r['blocked_reason']}" if r["blocked_reason"] else "")]
    out += [f"  !! {lbl}" for lbl in r["labels"]]
    s = r["snapshot"]
    out.append(f"snapshot {s['id']}  events={s['event_count']}  hash={s['content_hash'][:16]}..."
               f"  base skill set v{s['base_skill_set']['version']}")
    out.append("stages:")
    for st in r["stages"]:
        marks = ", ".join(f"fence{a['fence']}:{a['status']}" for a in st["attempts"]) or "not started"
        out.append(f"  {st['stage']:<12} {marks}")
    out.append(f"hypotheses ({len(r['hypotheses'])}):")
    for h in r["hypotheses"]:
        out.append(f"  H{h['ordinal']} [{h['origin']}] {h['claim']}")
        out.append(f"      falsifier: {h['falsifier']}")
        out.append(f"      sources: {', '.join('event:' + e for e in h['source_event_ids'])}")
    out.append(f"scenarios ({len(r['scenarios'])}):")
    for sc in r["scenarios"]:
        out.append(f"  S{sc['ordinal']} (H{sc['hypothesis']}) {sc['validity']:<12} {sc['title']} - {sc['validity_reason']}")
    out.append(f"experiments ({len(r['experiments'])}):")
    for x in r["experiments"]:
        out.append(f"  S{x['scenario']} attempt {x['attempt_no']}: {x['outcome']}  ({x['executor']}, mock={x['is_mock']})")
    out.append(f"candidates ({len(r['candidates'])}):")
    for c in r["candidates"]:
        gates = "; ".join(f"{g['decision']} by {g['validator']} (mock={g['is_mock']}, independent={g['independent']})"
                          for g in c["gate_decisions"]) or "no gate decision"
        out.append(f"  {c['slug']} v{c['version']} {c['skill_version_id']}  parent={c['parent_version_id']}")
        out.append(f"      hash={c['content_hash'][:16]}...  gate: {gates}")
        out.append(f"      evidence: {', '.join(c['evidence'])}")
    a = r["activation"]
    out.append("activation:")
    for o in a["outcomes"] or []:
        out.append(f"  {o['slug']}: {o['result']}" + (f" -> set v{o['new_set_version']}" if "new_set_version" in o else "")
                   + (f" ({o['detail']})" if "detail" in o else ""))
    out.append(f"  active set before run v{a['active_set_before_run']['version']}; now v{a['active_set_now']['version']}"
               f" (contains mock validation: {a['active_set_now']['contains_mock_validation']})")
    u = r["usage"]
    out.append(f"usage [{u['envelope']['unit']}; {u['unit_note']}]: limit={u['envelope']['limit_micro']}"
               f" held={u['envelope']['held_micro']} confirmed={u['envelope']['confirmed_micro']}"
               f" calls={len(u['provider_calls'])}")
    rs = r["real_spend"]
    out.append(f"real spend (CAD): confirmed={rs['confirmed_micro'] / 1e6:.2f} held={rs['held_micro'] / 1e6:.2f}")
    for al in r["budget_alerts"]:
        out.append(f"  !! BUDGET {al['kind'].upper()}: " + ", ".join(f"{k}={v}" for k, v in al.items() if k != "kind"))
    un = r["unresolved"]
    open_d = [d for d in un["billing_discrepancies"] if d["status"] == "open"]
    out.append(f"unresolved: {len(un['reservations'])} reservation(s); {len(un['outstanding_requests'])} outstanding"
               f" request(s); {len(open_d)} open billing discrepancy(ies); "
               f"{len(un['failed_or_abandoned_attempts'])} failed/abandoned attempt(s)")
    for f in un["failed_or_abandoned_attempts"]:
        out.append(f"  {f['stage']} fence{f['fence']} {f['status']}: {f['error']}")
    return "\n".join(out)


def _verify_call(conn: psycopg.Connection, artifacts: ArtifactStore, call_id: UUID) -> list[str]:
    pc = conn.execute("SELECT prompt_artifact, response_artifact FROM provider_calls WHERE id = %s",
                      (call_id,)).fetchone()
    out = [f"provider call {call_id} {kind} {pc[kind]} failed verification"
           for kind in ("prompt_artifact", "response_artifact") if pc[kind] is not None and not artifacts.verify(pc[kind])]
    if pc["prompt_artifact"] is None:
        out.append(f"provider call {call_id} has no recorded prompt (operator-recorded pre-0003 response)")
    return out


def trace_skill_version(conn: psycopg.Connection, artifacts: ArtifactStore, version_id: UUID) -> dict[str, Any]:
    """Walk a skill version back to its inputs, re-verifying every hash on the way."""
    problems: list[str] = []
    chain: dict[str, Any] = {"skill_version_id": str(version_id)}
    try:
        cand = load_candidate(conn, version_id)
        chain["content_hash"] = cand.content_hash
    except IntegrityFailure as exc:
        return {"ok": False, "problems": [str(exc)], "chain": chain}
    v = conn.execute("SELECT created_by_run_id FROM skill_versions WHERE id = %s", (version_id,)).fetchone()
    run = conn.execute("SELECT id, snapshot_id FROM sleep_runs WHERE id = %s", (v["created_by_run_id"],)).fetchone()
    if run is None:
        problems.append("skill version has no producing run")
        return {"ok": False, "problems": problems, "chain": chain}
    chain["run_id"] = str(run["id"])

    snap = conn.execute("SELECT * FROM snapshots WHERE id = %s", (run["snapshot_id"],)).fetchone()
    se = conn.execute("SELECT se.event_id, se.event_hash, e.event_hash AS live_hash, e.session_id FROM snapshot_events se"
                      " JOIN events e ON e.id = se.event_id JOIN sessions s ON s.id = e.session_id"
                      " WHERE se.snapshot_id = %s ORDER BY s.created_at, s.id, e.seq", (snap["id"],)).fetchall()
    if hash_obj({"base_skill_set_id": snap["base_skill_set_id"], "events": [e["event_hash"] for e in se]}) \
            != snap["content_hash"]:
        problems.append("snapshot content hash mismatch")
    for e in se:
        if e["event_hash"] != e["live_hash"]:
            problems.append(f"event {e['event_id']} hash differs from snapshot record")
    for sid in {e["session_id"] for e in se}:
        problems += [f"session {sid}: {p}" for p in verify_session_chain(conn, sid)]
    snap_events = {e["event_id"] for e in se}
    chain["snapshot"] = {"id": str(snap["id"]), "content_hash": snap["content_hash"], "events": len(se)}

    ev = conn.execute("SELECT event_id, hypothesis_id, experiment_run_id, provider_call_id FROM skill_version_evidence"
                      " WHERE skill_version_id = %s ORDER BY id", (version_id,)).fetchall()
    links: list[str] = []
    for row in ev:
        if row["event_id"]:
            if row["event_id"] not in snap_events:
                problems.append(f"evidence event {row['event_id']} is not in the run snapshot")
            links.append(f"event:{row['event_id']}")
        if row["hypothesis_id"]:
            h = conn.execute("SELECT run_id, provider_call_id FROM hypotheses WHERE id = %s",
                             (row["hypothesis_id"],)).fetchone()
            srcs = conn.execute("SELECT event_id FROM hypothesis_sources WHERE hypothesis_id = %s",
                                (row["hypothesis_id"],)).fetchall()
            if h["run_id"] != run["id"]:
                problems.append(f"hypothesis {row['hypothesis_id']} belongs to another run")
            if not srcs or any(s["event_id"] not in snap_events for s in srcs):
                problems.append(f"hypothesis {row['hypothesis_id']} cites events outside the snapshot")
            links.append(f"hypothesis:{row['hypothesis_id']} <- " + ",".join(f"event:{s['event_id']}" for s in srcs))
            if h["provider_call_id"]:
                problems += _verify_call(conn, artifacts, h["provider_call_id"])
                links.append(f"hypothesis:{row['hypothesis_id']} <- provider_call:{h['provider_call_id']}")
        if row["experiment_run_id"]:
            x = conn.execute("SELECT x.run_id, s.hypothesis_id, s.spec, s.spec_hash FROM experiment_runs x"
                             " JOIN scenarios s ON s.id = x.scenario_id WHERE x.id = %s",
                             (row["experiment_run_id"],)).fetchone()
            if x["run_id"] != run["id"]:
                problems.append(f"experiment {row['experiment_run_id']} belongs to another run")
            if hash_obj(x["spec"]) != x["spec_hash"]:
                problems.append(f"scenario spec hash mismatch for experiment {row['experiment_run_id']}")
            links.append(f"experiment_run:{row['experiment_run_id']} <- hypothesis:{x['hypothesis_id']}")
        if row["provider_call_id"]:
            problems += _verify_call(conn, artifacts, row["provider_call_id"])
            pc = conn.execute("SELECT reservation_id FROM provider_calls WHERE id = %s",
                              (row["provider_call_id"],)).fetchone()
            links.append(f"provider_call:{row['provider_call_id']} (reservation:{pc['reservation_id']})")
    if not any(r["experiment_run_id"] for r in ev):
        problems.append("no executed-experiment evidence")
    chain["evidence"] = links

    gates = conn.execute("SELECT id, candidate_hash, decision, is_mock, independent FROM gate_decisions"
                         " WHERE skill_version_id = %s", (version_id,)).fetchall()
    for g in gates:
        if g["candidate_hash"] != cand.content_hash:
            problems.append(f"gate decision {g['id']} refers to different content")
    chain["gate_decisions"] = [{k: _s(v) for k, v in g.items()} for g in gates]
    sets = conn.execute("SELECT s.id, s.version FROM skill_set_members m JOIN skill_sets s ON s.id = m.skill_set_id"
                        " WHERE m.skill_version_id = %s ORDER BY s.version", (version_id,)).fetchall()
    chain["member_of_sets"] = [{"id": str(s["id"]), "version": s["version"]} for s in sets]
    return {"ok": not problems, "problems": problems, "chain": chain}

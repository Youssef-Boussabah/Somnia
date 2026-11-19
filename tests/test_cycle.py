"""End-to-end mock cycle: traceability, fresh-session retrieval, immutability, labelling."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import psycopg
import pytest

from conftest import ROOT, make_run
from somnia.engine import SleepWorker
from somnia.errors import GateHashMismatch, ImmutableRecordError, translating
from somnia.events import verify_session_chain
from somnia.report import build_report, trace_skill_version
from somnia.skills import export_skill_set
from somnia.snapshots import create_snapshot


def _completed(conn, artifacts, project, **kw):
    run = make_run(conn, project["id"], **kw)
    assert SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"]).status == "completed"
    return run


def _cli(db_url, *args):
    env = {**os.environ, "SOMNIA_DATABASE_URL": db_url}
    p = subprocess.run([sys.executable, "-m", "somnia.cli", *args], cwd=ROOT, env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_full_cycle_report_is_labelled_and_complete(conn, artifacts, project):
    run = _completed(conn, artifacts, project)
    r = build_report(conn, run["run_id"])
    assert r["status"] == "completed" and r["mode"] == "mock"
    assert any("MOCK GATE" in lbl for lbl in r["labels"]) and any("NO RESEARCH RESULT" in lbl for lbl in r["labels"])
    assert [st["stage"] for st in r["stages"] if st["attempts"]] == [st["stage"] for st in r["stages"]]
    assert {c["slug"]: c["gate_decisions"][0]["decision"] for c in r["candidates"]} == {
        "durable-output-before-progress": "pass", "always-retry-on-failure": "fail"}
    assert all(g["is_mock"] and not g["independent"] for c in r["candidates"] for g in c["gate_decisions"])
    assert [s["validity"] for s in r["scenarios"]] == ["valid", "invalid"]
    assert r["activation"]["active_set_now"]["contains_mock_validation"] is True
    assert r["real_spend"] == {"limit_micro": 0, "held_micro": 0, "confirmed_micro": 0}
    assert r["unresolved"]["reservations"] == []
    # the frozen report artifact exists and verifies
    sha = conn.execute("SELECT output->>'report_artifact' AS s FROM stage_attempts WHERE run_id = %s"
                       " AND stage = 'report' AND status = 'succeeded'", (run["run_id"],)).fetchone()["s"]
    assert json.loads(artifacts.get(sha))["run_id"] == str(run["run_id"])


def _archived_report(conn, artifacts, run_id):
    sha = conn.execute("SELECT output->>'report_artifact' AS s FROM stage_attempts WHERE run_id = %s"
                       " AND stage = 'report' AND status = 'succeeded'", (run_id,)).fetchone()["s"]
    return json.loads(artifacts.get(sha).decode("utf-8"))  # the stored bytes, re-verified against their hash


def test_archived_report_describes_the_completed_run(conn, artifacts, project):
    run = _completed(conn, artifacts, project)
    archived = _archived_report(conn, artifacts, run["run_id"])
    assert archived["run_id"] == str(run["run_id"])
    assert archived["status"] == "completed"
    assert archived["current_stage"] == "done"
    report_stage = next(s for s in archived["stages"] if s["stage"] == "report")
    assert [a["status"] for a in report_stage["attempts"]] == ["succeeded"]
    assert all(a["status"] == "succeeded" for s in archived["stages"] for a in s["attempts"])
    assert archived["unresolved"] == {"reservations": [], "failed_or_abandoned_attempts": []}


def test_artifacts_trace_back_to_inputs(conn, artifacts, project):
    run = _completed(conn, artifacts, project)
    for v in conn.execute("SELECT id FROM skill_versions WHERE created_by_run_id = %s", (run["run_id"],)).fetchall():
        t = trace_skill_version(conn, artifacts, v["id"])
        assert t["ok"], t["problems"]
        kinds = {link.split(":")[0] for link in t["chain"]["evidence"]}
        assert {"event", "hypothesis", "experiment_run", "provider_call"} <= kinds
    # every hypothesis source is an event of the run's snapshot
    bad = conn.execute("SELECT count(*) AS n FROM hypotheses h JOIN hypothesis_sources hs ON hs.hypothesis_id = h.id"
                       " LEFT JOIN snapshot_events se ON se.event_id = hs.event_id AND se.snapshot_id = %s"
                       " WHERE h.run_id = %s AND se.event_id IS NULL", (run["snapshot_id"], run["run_id"])).fetchone()
    assert bad["n"] == 0


def test_trace_detects_tampering(conn, artifacts, project):
    run = _completed(conn, artifacts, project)
    v = conn.execute("SELECT v.id FROM skill_versions v JOIN skills s ON s.id = v.skill_id WHERE v.created_by_run_id = %s"
                     " AND s.slug = 'durable-output-before-progress'", (run["run_id"],)).fetchone()["id"]
    # 1) corrupt a stored provider response on disk
    pc = conn.execute("SELECT response_artifact FROM provider_calls WHERE run_id = %s ORDER BY created_at LIMIT 1",
                      (run["run_id"],)).fetchone()["response_artifact"]
    path = artifacts.root / pc[:2] / pc
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    assert not trace_skill_version(conn, artifacts, v)["ok"]
    path.write_bytes(original)
    assert trace_skill_version(conn, artifacts, v)["ok"]
    # 2) a superuser bypassing triggers to edit an event is detected by the hash chain
    conn.execute("SET session_replication_role = replica")
    try:
        conn.execute("UPDATE events SET payload = payload || '{\"tampered\": true}' WHERE session_id = %s AND seq = 2",
                     (run["session_id"],))
    finally:
        conn.execute("SET session_replication_role = origin")
    assert verify_session_chain(conn, run["session_id"]) == ["seq 2: payload hash mismatch"]
    assert not trace_skill_version(conn, artifacts, v)["ok"]


def test_fresh_session_retrieves_expected_active_version(conn, artifacts, project, db_url):
    run1 = _completed(conn, artifacts, project)
    set_v1 = conn.execute("SELECT skill_set_id FROM active_skill_sets WHERE project_id = %s",
                          (project["id"],)).fetchone()["skill_set_id"]
    snap2 = create_snapshot(conn, project["id"], [run1["session_id"]]).snapshot_id
    run2 = _completed(conn, artifacts, project, snapshot_id=snap2, script="indexer_checkpoint_revision_v1")
    v2 = conn.execute("SELECT id, version FROM skill_versions WHERE created_by_run_id = %s",
                      (run2["run_id"],)).fetchone()
    assert v2["version"] == 2

    args = ["retrieve", "--project", project["slug"], "--query", "resumable job writing results and a checkpoint",
            "--domain", "python-reliability", "--tags", "resumable", "local-persistence", "--json"]
    res = json.loads(_cli(db_url, *args))  # separate process, new connection, new session
    assert [s["skill_version_id"] for s in res["selected"]] == [str(v2["id"])]
    assert res["selected"][0]["version"] == 2 and res["skill_set_version"] == 2
    assert res["skill_set_contains_mock_validation"] is True
    log = conn.execute("SELECT s.purpose, r.selected FROM retrieval_logs r JOIN sessions s ON s.id = r.session_id"
                       " WHERE r.id = %s", (res["retrieval_log_id"],)).fetchone()
    assert log["purpose"] == "retrieval" and log["selected"] == [str(v2["id"])]

    # Scope rules: an excluded tag or another domain gets nothing, with a recorded reason.
    excl = json.loads(_cli(db_url, *args[:-1], "irreversible-external-effect", "--json"))
    assert excl["selected"] == [] and excl["excluded"][0]["reason"] == "excluded_by_scope"
    other = json.loads(_cli(db_url, "retrieve", "--project", project["slug"], "--query", "x", "--domain", "web",
                            "--json"))
    assert other["selected"] == [] and other["excluded"][0]["reason"] == "out_of_domain"

    # A frozen earlier set still returns its own version (reproducible retrieval).
    from somnia.contracts import RetrievalRequest, TaskScope
    from somnia.events import open_session
    from somnia.retrieval import retrieve_context
    sid = open_session(conn, project["id"], partition="development", origin="mock", purpose="retrieval",
                       agent_config_hash="t")
    old = retrieve_context(conn, RetrievalRequest(project_slug=project["slug"], session_id=sid, task_query="q",
                                                  scope=TaskScope(domain="python-reliability",
                                                                  tags=["resumable", "local-persistence"]),
                                                  skill_set_id=set_v1))
    assert [s.version for s in old.selected] == [1]


def test_skill_set_export_verifies(conn, artifacts, project):
    _completed(conn, artifacts, project)
    sid = conn.execute("SELECT skill_set_id FROM active_skill_sets WHERE project_id = %s",
                       (project["id"],)).fetchone()["skill_set_id"]
    exp = export_skill_set(conn, sid)
    assert exp.version == 1 and [m.skill_slug for m in exp.members] == ["durable-output-before-progress"]
    assert exp.members[0].gate_is_mock and not exp.members[0].gate_independent


def test_evidence_and_skill_records_are_immutable(conn, artifacts, project):
    run = _completed(conn, artifacts, project)
    stmts = [
        ("UPDATE skill_versions SET procedure = 'edited' WHERE created_by_run_id = %s", run["run_id"]),
        ("DELETE FROM gate_decisions WHERE run_id = %s", run["run_id"]),
        ("UPDATE gate_decisions SET decision = 'pass' WHERE run_id = %s", run["run_id"]),
        ("UPDATE events SET payload = '{}' WHERE session_id = %s", run["session_id"]),
        ("DELETE FROM skill_set_members WHERE skill_set_id IN (SELECT id FROM skill_sets WHERE project_id = %s)",
         project["id"]),
        ("UPDATE activations SET reason = 'x' WHERE run_id = %s", run["run_id"]),
        ("DELETE FROM audit_events WHERE project_id = %s", project["id"]),
        ("UPDATE snapshots SET event_count = 1 WHERE id = %s", run["snapshot_id"]),
    ]
    for sql, arg in stmts:
        with pytest.raises(ImmutableRecordError), translating():
            conn.execute(sql, (arg,))


def test_gate_records_cannot_lie_about_candidate_or_independence(conn, artifacts, project):
    run = _completed(conn, artifacts, project)
    g = conn.execute("SELECT * FROM gate_decisions WHERE run_id = %s LIMIT 1", (run["run_id"],)).fetchone()
    insert = ("INSERT INTO gate_decisions (skill_version_id, candidate_hash, baseline_skill_set_id, validator,"
              " validator_version, suite_id, suite_hash, independent, is_mock, decision, reason, paired_scores,"
              " evidence_refs) VALUES (%s, %s, %s, 'x', '1', 's', 'h', %s, %s, 'pass', 'r', '[]', '[]')")
    with pytest.raises(GateHashMismatch), translating():
        conn.execute(insert, (g["skill_version_id"], "0" * 64, g["baseline_skill_set_id"], False, False))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(insert, (g["skill_version_id"], g["candidate_hash"], g["baseline_skill_set_id"], True, True))

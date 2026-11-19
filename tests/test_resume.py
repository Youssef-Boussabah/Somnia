"""Interrupted runs resume without duplicate activation or duplicate spending."""

from __future__ import annotations

import pytest

from conftest import make_run, wait_lease_expired
from somnia import budget
from somnia.engine import SimulatedCrash, SleepWorker, unblock_run
from somnia.errors import InvalidReservationTransition, RunBlocked
from somnia.mock import MockProvider, load_mock_script
from somnia.report import build_report


def _crash_once(point: str, stage: str | None = None):
    state = {"fired": False}

    def hook(name, info):
        if name == point and not state["fired"] and (stage is None or info.get("stage", stage) == stage):
            state["fired"] = True
            raise SimulatedCrash(point)
    return hook, state


def test_crash_after_activation_resumes_without_duplicate_activation(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])
    hook, state = _crash_once("activation.after_attempt")
    a_conn = connect_db()
    with pytest.raises(SimulatedCrash):
        SleepWorker(a_conn, artifacts, worker_id="A", lease_ttl_s=1, hook=hook).work(run["run_id"])
    a_conn.close()  # the crashed process is gone
    assert state["fired"]

    # The activation transaction committed before the crash; the stage did not.
    assert conn.execute("SELECT current_stage FROM sleep_runs WHERE id = %s",
                        (run["run_id"],)).fetchone()["current_stage"] == "activate"
    assert conn.execute("SELECT count(*) AS n FROM activations WHERE run_id = %s",
                        (run["run_id"],)).fetchone()["n"] == 1

    wait_lease_expired(conn, run["run_id"])
    out = SleepWorker(connect_db(), artifacts, worker_id="B").work(run["run_id"])
    assert out.status == "completed"
    assert out.stages_run == ["activate", "report"]

    pid = project["id"]
    assert conn.execute("SELECT count(*) AS n FROM activations WHERE run_id = %s", (run["run_id"],)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM skill_sets WHERE project_id = %s", (pid,)).fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM audit_events WHERE project_id = %s AND action = 'skill_set.activate'",
                        (pid,)).fetchone()["n"] == 1
    active = conn.execute("SELECT s.version FROM active_skill_sets a JOIN skill_sets s ON s.id = a.skill_set_id"
                          " WHERE a.project_id = %s", (pid,)).fetchone()
    assert active["version"] == 1

    rep = build_report(conn, run["run_id"])
    assert [o["result"] for o in rep["activation"]["outcomes"]] == ["already_activated"]
    act_attempts = [a for s in rep["stages"] if s["stage"] == "activate" for a in s["attempts"]]
    assert [(a["worker_id"], a["status"]) for a in act_attempts] == [("A", "abandoned"), ("B", "succeeded")]
    assert budget.ledger_mismatches(conn, pid) == []


def test_crash_after_paid_call_reuses_completed_call_on_resume(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])
    hook, _ = _crash_once("gateway.after_settle")
    a = connect_db()
    with pytest.raises(SimulatedCrash):
        SleepWorker(a, artifacts, worker_id="A", lease_ttl_s=1, hook=hook).work(run["run_id"])
    a.close()
    wait_lease_expired(conn, run["run_id"])

    provider_b = MockProvider(load_mock_script("indexer_checkpoint_v1"))
    out = SleepWorker(connect_db(), artifacts, worker_id="B", provider=provider_b).work(run["run_id"])
    assert out.status == "completed"
    # B re-ran 'consolidate' but reused A's settled call: only the extract call was sent by B.
    assert len(provider_b.calls) == 1
    rows = conn.execute("SELECT state FROM reservations WHERE run_id = %s", (run["run_id"],)).fetchall()
    assert [r["state"] for r in rows] == ["settled", "settled"]
    assert conn.execute("SELECT count(*) AS n FROM provider_calls WHERE run_id = %s",
                        (run["run_id"],)).fetchone()["n"] == 2
    cons = conn.execute("SELECT output FROM stage_attempts WHERE run_id = %s AND stage = 'consolidate'"
                        " AND status = 'succeeded'", (run["run_id"],)).fetchone()["output"]
    assert cons["reused_call"] is True


def test_crash_between_reserve_and_send_blocks_until_reconciled(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])
    hook, _ = _crash_once("gateway.after_reserve")
    a = connect_db()
    with pytest.raises(SimulatedCrash):
        SleepWorker(a, artifacts, worker_id="A", lease_ttl_s=1, hook=hook).work(run["run_id"])
    a.close()
    wait_lease_expired(conn, run["run_id"])

    out = SleepWorker(connect_db(), artifacts, worker_id="B").work(run["run_id"])
    assert out.status == "blocked" and "uncertain_billing" in out.blocked_reason
    res = conn.execute("SELECT id, state, amount_micro FROM reservations WHERE run_id = %s",
                       (run["run_id"],)).fetchall()
    assert len(res) == 1 and res[0]["state"] == "uncertain"
    env = budget.envelope_status(conn, run["envelope_id"])
    assert env["reserved_micro"] == res[0]["amount_micro"]  # hold retained

    # Not even the worker can just release it, and the run cannot be unblocked yet.
    with pytest.raises(InvalidReservationTransition):
        budget.release_unsent(conn, res[0]["id"], note="guess")
    with pytest.raises(RunBlocked):
        unblock_run(conn, run["run_id"])
    assert SleepWorker(connect_db(), artifacts, worker_id="C").work(run["run_id"]).status == "blocked"

    budget.reconcile(conn, res[0]["id"], actual_micro=0, note="mock provider: no request recorded (dev test)")
    unblock_run(conn, run["run_id"])
    out = SleepWorker(connect_db(), artifacts, worker_id="D").work(run["run_id"])
    assert out.status == "completed"
    assert budget.ledger_mismatches(conn, project["id"]) == []
    assert budget.envelope_status(conn, run["envelope_id"])["reserved_micro"] == 0


def test_crash_during_report_stage_still_archives_an_accurate_report(connect_db, artifacts, project):
    import json

    conn = connect_db()
    run = make_run(conn, project["id"])
    hook, state = _crash_once("stage.before_commit", stage="report")
    a = connect_db()
    with pytest.raises(SimulatedCrash):
        SleepWorker(a, artifacts, worker_id="A", lease_ttl_s=1, hook=hook).work(run["run_id"])
    a.close()
    assert state["fired"]
    # Nothing of the report stage committed: the run is still at 'report' and no report is referenced.
    assert conn.execute("SELECT current_stage FROM sleep_runs WHERE id = %s",
                        (run["run_id"],)).fetchone()["current_stage"] == "report"

    wait_lease_expired(conn, run["run_id"])
    out = SleepWorker(connect_db(), artifacts, worker_id="B").work(run["run_id"])
    assert out.status == "completed" and out.stages_run == ["report"]
    shas = conn.execute("SELECT output->>'report_artifact' AS s FROM stage_attempts WHERE run_id = %s"
                        " AND stage = 'report' AND status = 'succeeded'", (run["run_id"],)).fetchall()
    assert len(shas) == 1
    archived = json.loads(artifacts.get(shas[0]["s"]).decode("utf-8"))
    assert (archived["status"], archived["current_stage"]) == ("completed", "done")
    report_stage = next(s for s in archived["stages"] if s["stage"] == "report")
    assert [(x["worker_id"], x["status"]) for x in report_stage["attempts"]] == [("A", "abandoned"), ("B", "succeeded")]


def test_completed_run_rework_is_a_no_op(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])
    assert SleepWorker(conn, artifacts, worker_id="A").work(run["run_id"]).status == "completed"
    before = conn.execute("SELECT count(*) AS n FROM activations WHERE project_id = %s", (project["id"],)).fetchone()["n"]
    assert SleepWorker(connect_db(), artifacts, worker_id="B").work(run["run_id"]).status == "completed"
    after = conn.execute("SELECT count(*) AS n FROM activations WHERE project_id = %s", (project["id"],)).fetchone()["n"]
    assert before == after


def test_run_creation_is_idempotent_and_key_cannot_be_reused(conn, project):
    from conftest import load_config
    from somnia.engine import create_run
    from somnia.errors import IdempotencyConflict

    run = make_run(conn, project["id"], key="same-key")
    again = create_run(conn, project_id=project["id"], snapshot_id=run["snapshot_id"], envelope_id=run["envelope_id"],
                       config=load_config(), idempotency_key="same-key")
    assert again == run["run_id"]
    with pytest.raises(IdempotencyConflict):
        create_run(conn, project_id=project["id"], snapshot_id=run["snapshot_id"], envelope_id=run["envelope_id"],
                   config=load_config(mock_script="indexer_checkpoint_revision_v1"), idempotency_key="same-key")

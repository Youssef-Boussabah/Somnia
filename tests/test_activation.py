"""Activation: stale parents and stale sets are rejected; activation is idempotent and auditable."""

from __future__ import annotations

import threading

import pytest

from conftest import load_config, make_run
from somnia.activation import activate_candidate, rollback_to
from somnia.engine import SleepWorker
from somnia.errors import IneligibleCandidate, StaleActivation, StaleParentVersion
from somnia.projects import active_skill_set_id
from somnia.snapshots import create_snapshot


def _first_run(conn, artifacts, project):
    run = make_run(conn, project["id"])
    assert SleepWorker(conn, artifacts, worker_id="w1").work(run["run_id"]).status == "completed"
    return run


def _version(conn, run_id, slug="durable-output-before-progress"):
    return conn.execute("SELECT v.id, v.version, v.parent_version_id FROM skill_versions v JOIN skills s"
                        " ON s.id = v.skill_id WHERE v.created_by_run_id = %s AND s.slug = %s",
                        (run_id, slug)).fetchone()


def _passing_gate(conn, run_id):
    return conn.execute("SELECT id FROM gate_decisions WHERE run_id = %s AND decision = 'pass'", (run_id,)).fetchone()["id"]


def test_stale_parent_version_cannot_be_promoted(conn, artifacts, project):
    run1 = _first_run(conn, artifacts, project)
    v1 = _version(conn, run1["run_id"])
    snap2 = create_snapshot(conn, project["id"], [run1["session_id"]]).snapshot_id  # base set now contains v1
    run_a = make_run(conn, project["id"], snapshot_id=snap2, script="indexer_checkpoint_revision_v1")
    run_b = make_run(conn, project["id"], snapshot_id=snap2, script="indexer_checkpoint_revision_v1")

    assert SleepWorker(conn, artifacts, worker_id="wa").work(run_a["run_id"]).status == "completed"
    assert SleepWorker(conn, artifacts, worker_id="wb").work(run_b["run_id"]).status == "completed"
    va, vb = _version(conn, run_a["run_id"]), _version(conn, run_b["run_id"])
    assert va["parent_version_id"] == v1["id"] and vb["parent_version_id"] == v1["id"]

    out_b = conn.execute("SELECT output FROM stage_attempts WHERE run_id = %s AND stage = 'activate'"
                         " AND status = 'succeeded'", (run_b["run_id"],)).fetchone()["output"]
    assert [o["result"] for o in out_b["outcomes"]] == ["stale_rejected"]

    active = active_skill_set_id(conn, project["id"])
    members = conn.execute("SELECT skill_version_id FROM skill_set_members WHERE skill_set_id = %s", (active,)).fetchall()
    assert [m["skill_version_id"] for m in members] == [va["id"]]
    assert conn.execute("SELECT count(*) AS n FROM skill_set_members WHERE skill_version_id = %s",
                        (vb["id"],)).fetchone()["n"] == 0

    # Even with an up-to-date expected set, B's candidate still revises a no-longer-active parent.
    with pytest.raises(StaleParentVersion):
        activate_candidate(conn, project_id=project["id"], gate_decision_id=_passing_gate(conn, run_b["run_id"]),
                           expected_previous_set_id=active, actor="test", idempotency_key="manual-b",
                           reason="test", allow_mock_validation=True)
    rejected = conn.execute("SELECT count(*) AS n FROM audit_events WHERE project_id = %s"
                            " AND action = 'skill_set.activate.rejected'", (project["id"],)).fetchone()["n"]
    assert rejected == 2


def test_concurrent_activations_on_same_baseline_admit_exactly_one(connect_db, artifacts, project):
    conn = connect_db()
    run1 = make_run(conn, project["id"], config=load_config(activation__auto_activate=False))
    run2 = make_run(conn, project["id"], snapshot_id=run1["snapshot_id"],
                    config=load_config(activation__auto_activate=False))
    for r in (run1, run2):
        assert SleepWorker(conn, artifacts, worker_id="w").work(r["run_id"]).status == "completed"
    base = active_skill_set_id(conn, project["id"])
    gates = [_passing_gate(conn, r["run_id"]) for r in (run1, run2)]
    barrier = threading.Barrier(2)
    results = []

    def go(i):
        c = connect_db()
        barrier.wait()
        try:
            results.append(activate_candidate(c, project_id=project["id"], gate_decision_id=gates[i],
                                              expected_previous_set_id=base, actor=f"t{i}", idempotency_key=f"k{i}",
                                              reason="race", allow_mock_validation=True))
        except (StaleActivation, StaleParentVersion) as exc:
            results.append(exc)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    applied = [r for r in results if not isinstance(r, Exception)]
    assert len(applied) == 1 and isinstance([r for r in results if isinstance(r, Exception)][0], StaleActivation)
    assert conn.execute("SELECT count(*) AS n FROM skill_sets WHERE project_id = %s",
                        (project["id"],)).fetchone()["n"] == 2


def test_activation_replay_is_idempotent(conn, artifacts, project):
    run = make_run(conn, project["id"], config=load_config(activation__auto_activate=False))
    SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"])
    base = active_skill_set_id(conn, project["id"])
    g = _passing_gate(conn, run["run_id"])
    kw = dict(project_id=project["id"], gate_decision_id=g, expected_previous_set_id=base, actor="t",
              idempotency_key="once", reason="t", allow_mock_validation=True)
    first = activate_candidate(conn, **kw)
    second = activate_candidate(conn, **kw)
    assert first.applied and not second.applied and first.new_set_id == second.new_set_id
    assert conn.execute("SELECT count(*) AS n FROM activations WHERE idempotency_key = 'once'").fetchone()["n"] == 1


def test_mock_gate_requires_explicit_permission_and_failed_gate_never_activates(conn, artifacts, project):
    run = make_run(conn, project["id"], config=load_config(activation__auto_activate=False))
    SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"])
    base = active_skill_set_id(conn, project["id"])
    with pytest.raises(IneligibleCandidate, match="mock"):
        activate_candidate(conn, project_id=project["id"], gate_decision_id=_passing_gate(conn, run["run_id"]),
                           expected_previous_set_id=base, actor="t", idempotency_key="nomock", reason="t",
                           allow_mock_validation=False)
    failed = conn.execute("SELECT id FROM gate_decisions WHERE run_id = %s AND decision = 'fail'",
                          (run["run_id"],)).fetchone()["id"]
    with pytest.raises(IneligibleCandidate, match="fail"):
        activate_candidate(conn, project_id=project["id"], gate_decision_id=failed, expected_previous_set_id=base,
                           actor="t", idempotency_key="failed", reason="t", allow_mock_validation=True)
    assert active_skill_set_id(conn, project["id"]) == base


def test_rollback_switches_pointer_and_keeps_history(conn, artifacts, project):
    _first_run(conn, artifacts, project)
    sets = conn.execute("SELECT id, version FROM skill_sets WHERE project_id = %s ORDER BY version",
                        (project["id"],)).fetchall()
    r = rollback_to(conn, project_id=project["id"], target_set_id=sets[0]["id"], expected_current_set_id=sets[1]["id"],
                    actor="t", idempotency_key="rb1", reason="test rollback")
    assert r.new_set_version == 0 and active_skill_set_id(conn, project["id"]) == sets[0]["id"]
    assert conn.execute("SELECT count(*) AS n FROM skill_sets WHERE project_id = %s",
                        (project["id"],)).fetchone()["n"] == 2

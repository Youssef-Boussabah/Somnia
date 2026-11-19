"""Activation: stale parents and stale sets are rejected; activation is idempotent and auditable."""

from __future__ import annotations

import threading
import uuid

import pytest

from conftest import load_config, make_run
from somnia.activation import activate_candidate, rollback_to
from somnia.contracts import GateRequest
from somnia.engine import SleepWorker
from somnia.errors import IdempotencyConflict, IneligibleCandidate, StaleActivation, StaleParentVersion
from somnia.mock import MockGate, load_mock_script
from somnia.projects import active_skill_set_id, create_project
from somnia.skills import export_skill_set, load_candidate, record_gate_decision
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
    # B's verdict was produced against v1, which is no longer active (D032: stale validation).
    assert [o["result"] for o in out_b["outcomes"]] == ["stale_validation"]

    active = active_skill_set_id(conn, project["id"])
    members = conn.execute("SELECT skill_version_id FROM skill_set_members WHERE skill_set_id = %s", (active,)).fetchall()
    assert [m["skill_version_id"] for m in members] == [va["id"]]
    assert conn.execute("SELECT count(*) AS n FROM skill_set_members WHERE skill_version_id = %s",
                        (vb["id"],)).fetchone()["n"] == 0

    # Even with an up-to-date expected set, B's old verdict is stale.
    with pytest.raises(StaleActivation, match="fresh validation"):
        activate_candidate(conn, project_id=project["id"], gate_decision_id=_passing_gate(conn, run_b["run_id"]),
                           expected_previous_set_id=active, actor="test", idempotency_key="manual-b",
                           reason="test", allow_mock_validation=True)
    # A fresh verdict against the current set does not help either: B still revises v1, which is no
    # longer the active version of the skill.
    verdict = MockGate(load_mock_script("indexer_checkpoint_revision_v1")).validate(GateRequest(
        candidate=load_candidate(conn, vb["id"]), baseline_skill_set=export_skill_set(conn, active),
        suite_id="mock-suite-v1"))
    fresh_b = record_gate_decision(conn, skill_version_id=vb["id"], baseline_skill_set_id=active, verdict=verdict,
                                   run_id=None)
    with pytest.raises(StaleParentVersion):
        activate_candidate(conn, project_id=project["id"], gate_decision_id=fresh_b, expected_previous_set_id=active,
                           actor="test", idempotency_key="manual-b-fresh", reason="test", allow_mock_validation=True)
    assert active_skill_set_id(conn, project["id"]) == active
    rejected = conn.execute("SELECT count(*) AS n FROM audit_events WHERE project_id = %s"
                            " AND action = 'skill_set.activate.rejected'", (project["id"],)).fetchone()["n"]
    assert rejected == 3


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


# ---------------------------------------------------------------- repairs (review of 4841892)
def _pointer(conn, pid):
    return conn.execute("SELECT skill_set_id, activation_id FROM active_skill_sets WHERE project_id = %s",
                        (pid,)).fetchone()


def _sets(conn, pid):
    return conn.execute("SELECT id, version, predecessor_id FROM skill_sets WHERE project_id = %s ORDER BY version",
                        (pid,)).fetchall()


def _n(conn, sql, *args):
    return conn.execute(sql, args).fetchone()["n"]


def test_activation_after_rollback_allocates_a_fresh_set_version(conn, artifacts, project):
    pid = project["id"]
    run1 = _first_run(conn, artifacts, project)  # v0 -> v1
    v0, v1 = _sets(conn, pid)
    # A different candidate, validated against v0 (run1's snapshot base), not auto-activated.
    run2 = make_run(conn, pid, snapshot_id=run1["snapshot_id"],
                    config=load_config(mock_script="indexer_checkpoint_revision_v1", activation__auto_activate=False))
    assert SleepWorker(conn, artifacts, worker_id="w2").work(run2["run_id"]).status == "completed"
    cand2 = _version(conn, run2["run_id"])
    assert cand2["parent_version_id"] is None

    rollback_to(conn, project_id=pid, target_set_id=v0["id"], expected_current_set_id=v1["id"], actor="t",
                idempotency_key="rb-v0", reason="test rollback")
    r = activate_candidate(conn, project_id=pid, gate_decision_id=_passing_gate(conn, run2["run_id"]),
                           expected_previous_set_id=v0["id"], actor="t", idempotency_key="after-rollback",
                           reason="test", allow_mock_validation=True)
    assert r.applied and r.new_set_version == 2 and r.previous_set_id == v0["id"]
    sets = _sets(conn, pid)
    assert [s["version"] for s in sets] == [0, 1, 2]
    assert sets[2]["id"] == r.new_set_id and sets[2]["predecessor_id"] == v0["id"]
    assert _pointer(conn, pid)["skill_set_id"] == r.new_set_id
    # The rolled-back v1 is untouched and still readable.
    assert [m.skill_version_id for m in export_skill_set(conn, v1["id"]).members] == [
        _version(conn, run1["run_id"])["id"]]
    assert [m.skill_version_id for m in export_skill_set(conn, r.new_set_id).members] == [cand2["id"]]


def _activate_outcomes(conn, run_id):
    return conn.execute("SELECT output FROM stage_attempts WHERE run_id = %s AND stage = 'activate'"
                        " AND status = 'succeeded'", (run_id,)).fetchone()["output"]["outcomes"]


def test_second_passing_candidate_on_same_baseline_needs_fresh_validation(conn, artifacts, project):
    pid = project["id"]
    run = make_run(conn, pid, script="two_passing_candidates_v1")
    assert SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"]).status == "completed"
    v0 = _sets(conn, pid)[0]
    outcomes = _activate_outcomes(conn, run["run_id"])
    assert [(o["slug"], o["result"]) for o in outcomes] == [("durable-output-before-progress", "activated"),
                                                            ("idempotent-reprocessing", "stale_validation")]
    assert "fresh validation" in outcomes[1]["detail"]
    active = _pointer(conn, pid)
    first, second = _version(conn, run["run_id"]), _version(conn, run["run_id"], "idempotent-reprocessing")
    assert [m.skill_version_id for m in export_skill_set(conn, active["skill_set_id"]).members] == [first["id"]]
    gates = {g["slug"]: g["id"] for g in conn.execute(
        "SELECT g.id, s.slug FROM gate_decisions g JOIN skill_versions v ON v.id = g.skill_version_id"
        " JOIN skills s ON s.id = v.skill_id WHERE g.run_id = %s", (run["run_id"],)).fetchall()}

    # The stale verdict stays unusable even when the caller names the up-to-date active set.
    with pytest.raises(StaleActivation, match="fresh validation") as exc:
        activate_candidate(conn, project_id=pid, gate_decision_id=gates["idempotent-reprocessing"],
                           expected_previous_set_id=active["skill_set_id"], actor="t", idempotency_key="stale-second",
                           reason="t", allow_mock_validation=True)
    assert type(exc.value).__name__ == "StaleValidation"
    assert _pointer(conn, pid) == active
    assert _n(conn, "SELECT count(*) AS n FROM audit_events WHERE project_id = %s"
                    " AND action = 'skill_set.activate.rejected'", pid) == 2

    # Fresh validation against the current active set makes the second candidate activatable.
    verdict = MockGate(load_mock_script("two_passing_candidates_v1")).validate(GateRequest(
        candidate=load_candidate(conn, second["id"]),
        baseline_skill_set=export_skill_set(conn, active["skill_set_id"]), suite_id="mock-suite-v1"))
    fresh = record_gate_decision(conn, skill_version_id=second["id"], baseline_skill_set_id=active["skill_set_id"],
                                 verdict=verdict, run_id=None)
    r = activate_candidate(conn, project_id=pid, gate_decision_id=fresh,
                           expected_previous_set_id=active["skill_set_id"], actor="t",
                           idempotency_key="fresh-second", reason="t", allow_mock_validation=True)
    assert r.applied and r.new_set_version == 2
    assert sorted(m.skill_slug for m in export_skill_set(conn, r.new_set_id).members) == [
        "durable-output-before-progress", "idempotent-reprocessing"]

    # Replaying the run's original, successful activation returns its recorded result and changes nothing.
    n_acts = _n(conn, "SELECT count(*) AS n FROM activations WHERE project_id = %s", pid)
    g1 = gates["durable-output-before-progress"]
    replay = activate_candidate(conn, project_id=pid, gate_decision_id=g1, expected_previous_set_id=v0["id"],
                                actor="t", idempotency_key=f"run:{run['run_id']}:gate:{g1}", reason="t",
                                allow_mock_validation=True)
    assert not replay.applied and replay.new_set_id == active["skill_set_id"] and replay.new_set_version == 1
    assert _n(conn, "SELECT count(*) AS n FROM activations WHERE project_id = %s", pid) == n_acts
    assert _pointer(conn, pid)["skill_set_id"] == r.new_set_id


def test_cross_project_activation_is_rejected_and_pointers_unchanged(conn, artifacts, project):
    pid = project["id"]
    run = make_run(conn, pid, config=load_config(activation__auto_activate=False))
    assert SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"]).status == "completed"
    other = create_project(conn, f"o-{uuid.uuid4().hex[:12]}")
    before = {p: _pointer(conn, p) for p in (pid, other)}
    gate = _passing_gate(conn, run["run_id"])

    with pytest.raises(IneligibleCandidate, match="project"):
        activate_candidate(conn, project_id=other, gate_decision_id=gate,
                           expected_previous_set_id=before[other]["skill_set_id"], actor="t",
                           idempotency_key="cross", reason="t", allow_mock_validation=True)
    assert {p: _pointer(conn, p) for p in before} == before
    assert _n(conn, "SELECT count(*) AS n FROM skill_sets WHERE project_id = %s", other) == 1
    assert _n(conn, "SELECT count(*) AS n FROM audit_events WHERE project_id = %s"
                    " AND action = 'skill_set.activate.rejected'", other) == 1

    # A key used for a legitimate activation in one project cannot be replayed into another.
    ok = activate_candidate(conn, project_id=pid, gate_decision_id=gate,
                            expected_previous_set_id=before[pid]["skill_set_id"], actor="t",
                            idempotency_key="legit", reason="t", allow_mock_validation=True)
    with pytest.raises(IdempotencyConflict):
        activate_candidate(conn, project_id=other, gate_decision_id=gate,
                           expected_previous_set_id=before[other]["skill_set_id"], actor="t",
                           idempotency_key="legit", reason="t", allow_mock_validation=True)
    assert _pointer(conn, pid)["skill_set_id"] == ok.new_set_id and _pointer(conn, other) == before[other]


# ---------------------------------------------------------------- review of b87707c: replay bound to operation kind
def _activate_v1_with_key_then_advance_to_v2(conn, artifacts, project, key):
    pid = project["id"]
    run1 = make_run(conn, pid, config=load_config(activation__auto_activate=False))
    assert SleepWorker(conn, artifacts, worker_id="w1").work(run1["run_id"]).status == "completed"
    v0 = _sets(conn, pid)[0]
    act_v1 = activate_candidate(conn, project_id=pid, gate_decision_id=_passing_gate(conn, run1["run_id"]),
                                expected_previous_set_id=v0["id"], actor="t", idempotency_key=key, reason="t",
                                allow_mock_validation=True)
    assert act_v1.applied and act_v1.new_set_version == 1
    snap2 = create_snapshot(conn, pid, [run1["session_id"]]).snapshot_id  # base set is v1
    run2 = make_run(conn, pid, snapshot_id=snap2, config=load_config(mock_script="indexer_checkpoint_revision_v1"))
    assert SleepWorker(conn, artifacts, worker_id="w2").work(run2["run_id"]).status == "completed"
    v2 = _pointer(conn, pid)["skill_set_id"]
    assert [s["version"] for s in _sets(conn, pid)] == [0, 1, 2] and v2 == _sets(conn, pid)[2]["id"]
    return act_v1, v2


def test_rollback_cannot_replay_an_activation_key(conn, artifacts, project):
    pid = project["id"]
    k = f"K-{uuid.uuid4().hex}"
    act_v1, v2 = _activate_v1_with_key_then_advance_to_v2(conn, artifacts, project, key=k)
    before = _pointer(conn, pid)
    n_acts = _n(conn, "SELECT count(*) AS n FROM activations WHERE project_id = %s", pid)

    # Same key, and the target equals the activation's new set: still a different operation.
    with pytest.raises(IdempotencyConflict, match="activate"):
        rollback_to(conn, project_id=pid, target_set_id=act_v1.new_set_id, expected_current_set_id=v2, actor="t",
                    idempotency_key=k, reason="rollback reusing an activation key")
    assert _pointer(conn, pid) == before and before["skill_set_id"] == v2
    assert _n(conn, "SELECT count(*) AS n FROM activations WHERE project_id = %s", pid) == n_acts


def test_rollback_retry_returns_original_receipt_and_activation_cannot_reuse_its_key(conn, artifacts, project):
    pid = project["id"]
    k = f"K-{uuid.uuid4().hex}"
    act_v1, v2 = _activate_v1_with_key_then_advance_to_v2(conn, artifacts, project, key=k)
    rb = f"RB-{uuid.uuid4().hex}"
    first = rollback_to(conn, project_id=pid, target_set_id=act_v1.new_set_id, expected_current_set_id=v2,
                        actor="t", idempotency_key=rb, reason="genuine rollback")
    assert first.applied and _pointer(conn, pid)["skill_set_id"] == act_v1.new_set_id
    # A retry (same key, same target) returns the original rollback receipt, even with a stale expected set.
    retry = rollback_to(conn, project_id=pid, target_set_id=act_v1.new_set_id, expected_current_set_id=v2,
                        actor="t", idempotency_key=rb, reason="retry")
    assert not retry.applied
    assert (retry.activation_id, retry.previous_set_id, retry.new_set_id, retry.new_set_version) == (
        first.activation_id, first.previous_set_id, first.new_set_id, first.new_set_version)
    assert _n(conn, "SELECT count(*) AS n FROM activations WHERE idempotency_key = %s", rb) == 1
    # The rollback's key cannot be replayed as an activation either.
    with pytest.raises(IdempotencyConflict, match="rollback"):
        activate_candidate(conn, project_id=pid, gate_decision_id=_passing_gate(conn, _run_of(conn, act_v1)),
                           expected_previous_set_id=act_v1.new_set_id, actor="t", idempotency_key=rb,
                           reason="t", allow_mock_validation=True)
    assert _pointer(conn, pid)["skill_set_id"] == act_v1.new_set_id


def _run_of(conn, act):
    return conn.execute("SELECT g.run_id FROM activations a JOIN gate_decisions g ON g.id = a.gate_decision_id"
                        " WHERE a.id = %s", (act.activation_id,)).fetchone()["run_id"]

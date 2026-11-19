"""Stage leases: an expired worker cannot overwrite a newer worker's progress."""

from __future__ import annotations

import threading

import pytest

from conftest import make_run, wait_lease_expired
from somnia.engine import SleepWorker
from somnia.errors import LeaseLost, LeaseUnavailable


def test_expired_worker_cannot_overwrite_newer_worker(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])
    rid = run["run_id"]
    b_conn = connect_db()
    seen = {}

    def hook_a(name, info):
        # Worker A has done all of consolidate's work and is about to commit. It stalls past its lease;
        # worker B takes over, redoes and commits the stage.
        if name == "stage.before_commit" and info["stage"] == "consolidate" and "b" not in seen:
            wait_lease_expired(b_conn, rid)
            seen["b"] = SleepWorker(b_conn, artifacts, worker_id="B", lease_ttl_s=30).work(rid, max_stages=1)

    a = SleepWorker(connect_db(), artifacts, worker_id="A", lease_ttl_s=1, hook=hook_a)
    with pytest.raises(LeaseLost):
        a.work(rid)

    assert seen["b"].stages_run == ["consolidate"]
    attempts = conn.execute("SELECT id, worker_id, fence, status FROM stage_attempts WHERE run_id = %s"
                            " AND stage = 'consolidate' ORDER BY fence", (rid,)).fetchall()
    assert [(x["worker_id"], x["fence"], x["status"]) for x in attempts] == [("A", 1, "abandoned"),
                                                                            ("B", 2, "succeeded")]
    # Every hypothesis belongs to B's attempt; A's writes were rolled back.
    owners = conn.execute("SELECT DISTINCT stage_attempt_id FROM hypotheses WHERE run_id = %s", (rid,)).fetchall()
    assert [o["stage_attempt_id"] for o in owners] == [attempts[1]["id"]]
    assert conn.execute("SELECT count(*) AS n FROM hypotheses WHERE run_id = %s", (rid,)).fetchone()["n"] == 2
    state = conn.execute("SELECT current_stage, fence FROM sleep_runs WHERE id = %s", (rid,)).fetchone()
    assert (state["current_stage"], state["fence"]) == ("scenarios", 2)
    # A's call was settled by A and reused by B rather than paid twice.
    assert conn.execute("SELECT count(*) AS n FROM provider_calls WHERE run_id = %s", (rid,)).fetchone()["n"] == 1

    # The run still completes correctly afterwards.
    assert SleepWorker(connect_db(), artifacts, worker_id="C").work(rid).status == "completed"


def test_expired_lease_alone_prevents_commit(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])

    def hook(name, info):
        if name == "stage.before_commit":
            wait_lease_expired(conn, run["run_id"])

    with pytest.raises(LeaseLost):
        SleepWorker(connect_db(), artifacts, worker_id="A", lease_ttl_s=1, hook=hook).work(run["run_id"])
    assert conn.execute("SELECT current_stage FROM sleep_runs WHERE id = %s",
                        (run["run_id"],)).fetchone()["current_stage"] == "consolidate"
    assert conn.execute("SELECT count(*) AS n FROM hypotheses WHERE run_id = %s", (run["run_id"],)).fetchone()["n"] == 0


def test_expired_worker_cannot_reserve_budget(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])

    def hook(name, info):
        if name == "stage.before_commit" and info["stage"] == "scenarios":
            wait_lease_expired(conn, run["run_id"])
    # Lease expires between stages; the heartbeat at the next stage start must fail before any reservation.
    with pytest.raises(LeaseLost):
        SleepWorker(connect_db(), artifacts, worker_id="A", lease_ttl_s=1, hook=hook).work(run["run_id"])
    n = conn.execute("SELECT count(*) AS n FROM reservations WHERE run_id = %s", (run["run_id"],)).fetchone()["n"]
    assert n == 1  # only consolidate's call; nothing reserved after expiry


def test_only_one_of_two_concurrent_claimers_wins(connect_db, artifacts, project):
    conn = connect_db()
    run = make_run(conn, project["id"])
    workers = [SleepWorker(connect_db(), artifacts, worker_id=f"W{i}", lease_ttl_s=30) for i in range(2)]
    barrier = threading.Barrier(2)
    results: list[object] = []

    def claim(w):
        barrier.wait()
        try:
            results.append(w._claim(run["run_id"]))
        except LeaseUnavailable as exc:
            results.append(exc)

    ts = [threading.Thread(target=claim, args=(w,)) for w in workers]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    fences = [r for r in results if isinstance(r, int)]
    assert fences == [1] and sum(isinstance(r, LeaseUnavailable) for r in results) == 1

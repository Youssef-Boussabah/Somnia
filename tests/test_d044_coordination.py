"""D044 follow-up (Codex review): coordinated decisions per logical request, hard-cap coverage while a
reconciled request is still outstanding, and the pre-send boundary. Mocks + real PostgreSQL only.

"Logical request" = (run, request key): every send attempt of the same prompt/template/model in a run.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from conftest import EXPERIENCE, load_config, make_run, wait_lease_expired
from somnia import budget
from somnia.engine import SleepWorker, create_run, unblock_run
from somnia.errors import BudgetExceeded, InvalidRequestResolution
from somnia.events import record_fixture_session
from somnia.mock import MockProvider, load_mock_script
from somnia.snapshots import create_snapshot
from test_gateway_recovery import SCRIPT, GatedProvider, Scenario, n

UNIT = 200  # 1 test "unit" = 200 MOCK_MICRO: A's consolidate hold of 16,000 is exactly 80 units


def pause_once(point, stage="consolidate"):
    """Hook that blocks the worker the first time it reaches ``point`` (for ``stage``) until released."""
    reached, go, fired = threading.Event(), threading.Event(), []

    def hook(name, info):
        if name == point and not fired and info.get("stage", stage) == stage:
            fired.append(name)
            reached.set()
            assert go.wait(60), f"paused worker at {point} was never released"
    return hook, reached, go


def in_thread(fn):
    out: dict = {}

    def run():
        try:
            out["result"] = fn()
        except BaseException as exc:  # reported by assertions
            out["error"] = exc
    t = threading.Thread(target=run)
    t.start()
    return t, out


def consolidate_key(conn, hold):
    return conn.execute("SELECT request_key FROM provider_requests WHERE reservation_id = %s", (hold,)).fetchone()[
        "request_key"]


def sends_of(key, *providers):
    return sum(p.calls.count(key) for p in providers)


def _a_in_flight_then_exact(connect_db, artifacts, project):
    pid = project["id"]
    s = Scenario(connect_db, artifacts, make_run(connect_db(), pid), GatedProvider())
    budget.reconcile(s.conn, s.hold, actual_micro=s.late_usage, note="operator: exact amount from usage export")
    unblock_run(s.conn, s.rid)
    return s


# =============================================================== 1. reuse / block / reserve is one decision per logical request
def test_replacement_paused_after_lookup_reuses_the_late_exact_response(connect_db, artifacts, project):
    """C misses the completed-response lookup, pauses; A's exact-amount late response commits; C resumes."""
    s = _a_in_flight_then_exact(connect_db, artifacts, project)
    key = consolidate_key(s.conn, s.hold)
    hook, reached, go = pause_once("gateway.after_reuse_lookup")
    prov_c = MockProvider(load_mock_script(SCRIPT))
    tc, c = in_thread(lambda: SleepWorker(connect_db(), artifacts, worker_id="C", provider=prov_c, hook=hook).work(s.rid))
    assert reached.wait(30)
    s.deliver()  # A records its response (committed) and then fails to commit its stage
    go.set()
    tc.join(60)
    assert "error" not in c, c
    assert c["result"].status == "completed"
    # C reused A's receipt: no new reservation or request for the logical request, no second send.
    assert n(s.conn, "SELECT count(*) AS n FROM provider_requests WHERE request_key = %s", key) == 1
    assert n(s.conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s AND idempotency_key LIKE %s",
             s.rid, key + ":%") == 1
    assert sends_of(key, s.prov, prov_c) == 1 and prov_c.calls.count(key) == 0
    receipt = s.receipt_for_a()
    cons = s.conn.execute("SELECT output FROM stage_attempts WHERE run_id = %s AND stage = 'consolidate'"
                          " AND status = 'succeeded'", (s.rid,)).fetchone()["output"]
    assert cons["reused_call"] is True and cons["provider_call_id"] == str(receipt["id"])
    assert budget.open_discrepancies(s.conn, project["id"]) == []
    assert budget.ledger_mismatches(s.conn, project["id"]) == []


def test_late_response_cannot_commit_between_admission_checks(connect_db, artifacts, project):
    """C is inside its admission decision (after the completion re-check, before the outstanding check) when
    A's response arrives. Two reads in one transaction would let A commit in between and C send again."""
    s = _a_in_flight_then_exact(connect_db, artifacts, project)
    key = consolidate_key(s.conn, s.hold)
    hook, reached, go = pause_once("gateway.admission_after_completion_check")
    prov_c = MockProvider(load_mock_script(SCRIPT))
    tc, c = in_thread(lambda: SleepWorker(connect_db(), artifacts, worker_id="C", provider=prov_c, hook=hook).work(s.rid))
    assert reached.wait(30)
    s.prov.deliver.set()  # A now tries to record its response while C holds the decision
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and s.thread.is_alive():
        if n(s.conn, "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"):
            break  # A is waiting for C's decision on the same logical request
        time.sleep(0.05)
    go.set()
    tc.join(60)
    s.thread.join(60)
    assert "error" not in c, c
    assert type(s.out.get("a_err")).__name__ == "LeaseLost"
    assert sends_of(key, s.prov, prov_c) == 1  # never a second send of the logical request
    assert n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE request_key = %s", key) == 1
    assert n(s.conn, "SELECT count(*) AS n FROM provider_requests WHERE request_key = %s", key) == 1
    if c["result"].status == "blocked":  # C decided first: the request was still outstanding
        assert c["result"].blocked_reason.startswith("request_outstanding")
        unblock_run(s.conn, s.rid)
        prov_d = MockProvider(load_mock_script(SCRIPT))
        assert SleepWorker(connect_db(), artifacts, worker_id="D", provider=prov_d).work(s.rid).status == "completed"
        assert prov_d.calls.count(key) == 0
    else:
        assert c["result"].status == "completed"
    assert budget.ledger_mismatches(s.conn, project["id"]) == []


# =============================================================== 2. hard cap across siblings while a request is outstanding
@pytest.mark.parametrize("estimate", ["zero", "lower", "exact", "higher"])
def test_reconciled_but_outstanding_request_keeps_cap_coverage(connect_db, artifacts, project, estimate):
    conn, pid = connect_db(), project["id"]
    parent = budget.create_envelope(conn, pid, "parent", unit="MOCK_MICRO", limit_micro=100 * UNIT)
    leaf = budget.create_envelope(conn, pid, "leaf-a", unit="MOCK_MICRO", limit_micro=100 * UNIT, parent_id=parent)
    sibling = budget.create_envelope(conn, pid, "sibling", unit="MOCK_MICRO", limit_micro=100 * UNIT, parent_id=parent)
    session, _ = record_fixture_session(conn, pid, EXPERIENCE)
    rid = create_run(conn, project_id=pid, snapshot_id=create_snapshot(conn, pid, [session]).snapshot_id,
                     envelope_id=leaf, config=load_config(), idempotency_key=f"cap-{uuid.uuid4().hex}")
    s = Scenario(connect_db, artifacts, {"run_id": rid, "envelope_id": leaf}, GatedProvider())
    hold = conn.execute("SELECT amount_micro FROM reservations WHERE id = %s", (s.hold,)).fetchone()["amount_micro"]
    assert hold == 80 * UNIT  # A reserved 80 of the 100-unit parent
    u = s.late_usage
    x = {"zero": 0, "lower": u // 2, "exact": u, "higher": 90 * UNIT}[estimate]
    budget.reconcile(conn, s.hold, actual_micro=x, note=f"operator estimate ({estimate}) while response outstanding")

    def st(env):
        e = budget.envelope_status(conn, env)
        return e["reserved_micro"], e["confirmed_micro"]
    remaining = max(hold - x, 0)  # what A can still bill beyond the operator's figure, up to its bound
    assert st(leaf) == st(parent) == (remaining, x), (st(leaf), st(parent))
    assert st(sibling) == (0, 0)

    def reserve(env, amount):
        return budget.reserve(conn, envelope_id=env, amount_micro=amount, idempotency_key=uuid.uuid4().hex,
                              purpose="sibling", provider="mock", model="m", pricing_snapshot={})
    with pytest.raises(BudgetExceeded):  # the sibling cannot take the whole parent while A can still bill
        reserve(sibling, 100 * UNIT)
    room = 100 * UNIT - max(hold, x)
    with pytest.raises(BudgetExceeded):
        reserve(sibling, room + 1)
    budget.release_unsent(conn, reserve(sibling, room).id, note="probe, never sent")
    assert budget.ledger_mismatches(conn, pid) == []

    s.deliver()  # A's response: the remaining coverage is released and the charge is counted once
    assert st(leaf) == st(parent) == (0, u), (st(leaf), st(parent))
    assert st(sibling) == (0, 0)
    disc = budget.open_discrepancies(conn, pid)
    if x == u:
        assert disc == []
    else:
        assert [(d["operator_micro"], d["reported_micro"], d["delta_micro"]) for d in disc] == [(x, u, u - x)]
    assert budget.ledger_mismatches(conn, pid) == []


# =============================================================== pre-send boundary
def test_sender_paused_before_sending_cannot_send_after_abandonment(connect_db, artifacts, project):
    """A's reservation committed; A pauses before provider.complete. Its hold is orphaned, reconciled and
    abandoned; replacement C retries and sends. When A wakes it must not send as well."""
    conn = connect_db()
    run = make_run(conn, project["id"])
    rid = run["run_id"]
    hook, reached, go = pause_once("gateway.after_reserve")
    prov_a = MockProvider(load_mock_script(SCRIPT))
    ta, a = in_thread(lambda: SleepWorker(connect_db(), artifacts, worker_id="A", lease_ttl_s=1, provider=prov_a,
                                          hook=hook).work(rid))
    assert reached.wait(30)
    wait_lease_expired(conn, rid)
    assert SleepWorker(connect_db(), artifacts, worker_id="B").work(rid).status == "blocked"
    hold = conn.execute("SELECT id FROM reservations WHERE run_id = %s", (rid,)).fetchone()["id"]
    key = consolidate_key(conn, hold)
    budget.reconcile(conn, hold, actual_micro=0, note="operator: no usage for this window")
    budget.abandon_request(conn, hold, actor="op",
                           evidence="provider request log shows no request with this request key")
    unblock_run(conn, rid)
    prov_c = MockProvider(load_mock_script(SCRIPT))
    assert SleepWorker(connect_db(), artifacts, worker_id="C", provider=prov_c).work(rid).status == "completed"
    assert prov_c.calls.count(key) == 1  # the permitted retry

    go.set()  # A wakes up
    ta.join(60)
    assert prov_a.calls == []  # A never sent: its send permit was refused
    assert type(a.get("error")).__name__ in ("LeaseLost", "InvalidRequestResolution"), a
    assert sends_of(key, prov_a, prov_c) == 1
    assert budget.ledger_mismatches(conn, project["id"]) == []


def test_open_send_permit_requires_sender_stopped_attestation_to_abandon(connect_db, artifacts, project):
    """A holds a send permit and is mid-send (provider has the request; response not delivered). Provider-side
    evidence alone cannot show that A will not still send/complete, so abandonment is refused."""
    s = Scenario(connect_db, artifacts, make_run(connect_db(), project["id"]), GatedProvider())
    key = consolidate_key(s.conn, s.hold)
    budget.reconcile(s.conn, s.hold, actual_micro=0, note="operator: no usage visible yet")
    with pytest.raises(InvalidRequestResolution, match="sender"):
        budget.abandon_request(s.conn, s.hold, actor="op", evidence="provider request log shows no request yet")
    assert n(s.conn, "SELECT count(*) AS n FROM provider_request_abandonments WHERE reservation_id = %s", s.hold) == 0
    unblock_run(s.conn, s.rid)
    out, sent = s.worker("C")
    assert out.status == "blocked" and out.blocked_reason.startswith("request_outstanding") and sent == 0
    s.deliver()  # A's request did complete after all
    [d] = budget.open_discrepancies(s.conn, project["id"])
    budget.resolve_discrepancy(s.conn, d["id"], note="acknowledged", actor="op")
    unblock_run(s.conn, s.rid)
    prov_d = MockProvider(load_mock_script(SCRIPT))
    assert SleepWorker(connect_db(), artifacts, worker_id="D", provider=prov_d).work(s.rid).status == "completed"
    assert sends_of(key, s.prov, prov_d) == 1
    assert budget.ledger_mismatches(s.conn, project["id"]) == []


def test_sender_paused_before_sending_cannot_send_after_losing_its_lease(connect_db, artifacts, project):
    """No abandonment here: A's lease simply expired while it was paused before sending, and B took over.
    The lease-fenced send permit alone must stop A, leaving its request provably unsent (no permit)."""
    conn = connect_db()
    run = make_run(conn, project["id"])
    rid = run["run_id"]
    hook, reached, go = pause_once("gateway.after_reserve")
    prov_a = MockProvider(load_mock_script(SCRIPT))
    ta, a = in_thread(lambda: SleepWorker(connect_db(), artifacts, worker_id="A", lease_ttl_s=1, provider=prov_a,
                                          hook=hook).work(rid))
    assert reached.wait(30)
    wait_lease_expired(conn, rid)
    assert SleepWorker(connect_db(), artifacts, worker_id="B").work(rid).status == "blocked"
    go.set()
    ta.join(60)
    assert type(a.get("error")).__name__ == "LeaseLost" and prov_a.calls == []
    hold = conn.execute("SELECT id, state FROM reservations WHERE run_id = %s", (rid,)).fetchone()
    assert hold["state"] == "uncertain"
    assert n(conn, "SELECT count(*) AS n FROM provider_request_sends WHERE reservation_id = %s", hold["id"]) == 0
    # With no permit, nothing was sent: abandonment needs no sender attestation, and the retry is safe.
    budget.reconcile(conn, hold["id"], actual_micro=0, note="nothing was sent (no send permit)")
    budget.abandon_request(conn, hold["id"], actor="op", evidence="no send permit was ever granted for this request")
    unblock_run(conn, rid)
    prov_c = MockProvider(load_mock_script(SCRIPT))
    assert SleepWorker(connect_db(), artifacts, worker_id="C", provider=prov_c).work(rid).status == "completed"
    key = consolidate_key(conn, hold["id"])
    assert sends_of(key, prov_a, prov_c) == 1
    assert budget.ledger_mismatches(conn, project["id"]) == []

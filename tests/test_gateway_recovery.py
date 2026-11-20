"""Gateway recovery after lease loss: billing reconciliation and request completion are separate facts.

Scenario used throughout (mock provider, real PostgreSQL):
  * worker A reserves and sends consolidate's call; the provider does (and bills) the work, but the
    response reaches A only after A's 1 s lease has expired;
  * worker B claims the run, marks A's hold 'uncertain' and blocks the run;
  * then, depending on the test, an operator reconciles the hold, the run is unblocked, a replacement
    worker resumes, and A's response arrives -- in different orders.
Invariants checked: provider send counts, durable receipts (provider_calls), stage state, ledger totals.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from conftest import EXPERIENCE, load_config, make_run, wait_lease_expired
from somnia import budget
from somnia.engine import SimulatedCrash, SleepWorker, create_run, unblock_run
from somnia.events import record_fixture_session
from somnia.interfaces import ProviderResponse
from somnia.mock import MockProvider, load_mock_script
from somnia.snapshots import create_snapshot

SCRIPT = "indexer_checkpoint_v1"


class GatedProvider(MockProvider):
    """Mock provider whose FIRST call is completed (and billed) at once, but whose response is
    delivered to the caller only when ``deliver`` is set. ``usage_override`` changes the reported tokens."""

    def __init__(self, usage_override: tuple[int, int] | None = None):
        super().__init__(load_mock_script(SCRIPT))
        self.computed, self.deliver, self.responses = threading.Event(), threading.Event(), []
        self.usage_override = usage_override

    def complete(self, **kw):
        resp = super().complete(**kw)
        if self.usage_override and not self.responses:
            resp = ProviderResponse(resp.text, *self.usage_override, resp.request_id)
        self.responses.append(resp)
        if len(self.responses) == 1:
            self.computed.set()
            assert self.deliver.wait(30)
        return resp


def usage(resp: ProviderResponse) -> int:
    g = load_config().gateway
    return resp.input_tokens * g.input_micro_per_token + resp.output_tokens * g.output_micro_per_token


def n(conn, sql, *args) -> int:
    return conn.execute(sql, args).fetchone()["n"]


class Scenario:
    """A has sent consolidate's call and lost its lease; B has blocked the run on the uncertain hold."""

    def __init__(self, connect_db, artifacts, run, provider: GatedProvider):
        self.connect_db, self.artifacts, self.run, self.prov = connect_db, artifacts, run, provider
        self.conn = connect_db()
        self.rid = run["run_id"]
        self.out: dict = {}
        self.thread = threading.Thread(target=self._worker_a)
        self.thread.start()
        assert provider.computed.wait(30)
        wait_lease_expired(self.conn, self.rid)
        b = SleepWorker(connect_db(), artifacts, worker_id="B").work(self.rid)
        assert b.status == "blocked" and b.blocked_reason.startswith("uncertain_billing")
        holds = self.conn.execute("SELECT id, state FROM reservations WHERE run_id = %s", (self.rid,)).fetchall()
        assert [h["state"] for h in holds] == ["uncertain"]
        self.hold = holds[0]["id"]
        self.a_attempt = self.conn.execute("SELECT stage_attempt_id FROM reservations WHERE id = %s",
                                           (self.hold,)).fetchone()["stage_attempt_id"]

    def _worker_a(self):
        try:
            self.out["a"] = SleepWorker(self.connect_db(), self.artifacts, worker_id="A", lease_ttl_s=1,
                                        provider=self.prov).work(self.rid)
        except BaseException as exc:
            self.out["a_err"] = exc

    @property
    def late_usage(self) -> int:
        return usage(self.prov.responses[0])

    def deliver(self):
        self.prov.deliver.set()
        self.thread.join(30)
        assert not self.thread.is_alive()
        # A records the evidence but can never advance the stage it no longer holds.
        assert type(self.out.get("a_err")).__name__ == "LeaseLost", self.out

    def run_state(self):
        r = self.conn.execute("SELECT current_stage, status FROM sleep_runs WHERE id = %s", (self.rid,)).fetchone()
        return r["current_stage"], r["status"]

    def worker(self, wid):
        prov = MockProvider(load_mock_script(SCRIPT))
        out = SleepWorker(self.connect_db(), self.artifacts, worker_id=wid, provider=prov).work(self.rid)
        return out, len(prov.calls)

    def receipt_for_a(self):
        return self.conn.execute("SELECT id, stage_attempt_id, request_id, input_tokens, output_tokens FROM provider_calls"
                                 " WHERE reservation_id = %s", (self.hold,)).fetchone()

    def attempts(self, stage):
        return [(a["worker_id"], a["status"]) for a in self.conn.execute(
            "SELECT worker_id, status FROM stage_attempts WHERE run_id = %s AND stage = %s ORDER BY fence",
            (self.rid, stage)).fetchall()]

    def confirmed(self, envelope_id=None):
        return budget.envelope_status(self.conn, envelope_id or self.run["envelope_id"])["confirmed_micro"]

    def settled_total(self):
        return self.conn.execute("SELECT COALESCE(SUM(actual_micro), 0) AS s FROM reservations WHERE run_id = %s"
                                 " AND state = 'settled'", (self.rid,)).fetchone()["s"]


def _scenario(connect_db, artifacts, project, provider=None, **make_run_kw):
    conn = connect_db()
    return Scenario(connect_db, artifacts, make_run(conn, project["id"], **make_run_kw), provider or GatedProvider())


def _reconcile(s: Scenario, amount: str):
    budget.reconcile(s.conn, s.hold, actual_micro=0 if amount == "zero" else s.late_usage,
                     note=f"operator billing reconciliation ({amount}) from provider usage export")


# ---------------------------------------------------------------- T1 (promoted): response before resume, no operator
def test_late_response_before_resume_settles_uncertain_hold_and_is_reused(connect_db, artifacts, project):
    s = _scenario(connect_db, artifacts, project)
    s.deliver()
    hold = s.conn.execute("SELECT state, actual_micro FROM reservations WHERE id = %s", (s.hold,)).fetchone()
    assert (hold["state"], hold["actual_micro"]) == ("settled", s.late_usage)
    rec = s.receipt_for_a()
    assert rec is not None and rec["stage_attempt_id"] == s.a_attempt and rec["request_id"] == s.prov.responses[0].request_id
    assert s.run_state() == ("consolidate", "blocked")

    unblock_run(s.conn, s.rid)
    out, sent = s.worker("C")
    assert out.status == "completed" and sent == 1  # extract only; consolidate's late response reused
    assert len(s.prov.responses) == 1
    assert s.attempts("consolidate") == [("A", "abandoned"), ("C", "succeeded")]
    assert n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE run_id = %s", s.rid) == 2
    assert s.confirmed() == s.settled_total() and budget.ledger_mismatches(s.conn, project["id"]) == []


# ---------------------------------------------------------------- R1 (promoted): reconciliation, then response, then resume
@pytest.mark.parametrize("amount", ["exact", "zero"])
def test_late_response_after_operator_reconciliation_is_recorded_and_reused(connect_db, artifacts, project, amount):
    s = _scenario(connect_db, artifacts, project)
    _reconcile(s, amount)
    s.deliver()
    rec = s.receipt_for_a()
    assert rec is not None and rec["stage_attempt_id"] == s.a_attempt  # evidence kept after reconciliation
    assert n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE run_id = %s", s.rid) == 1
    assert s.confirmed() == s.late_usage  # zero: operator 0 + the difference, accounted once
    unblock_run(s.conn, s.rid)
    if amount == "exact":
        assert budget.open_discrepancies(s.conn, project["id"]) == []
        out, sent = s.worker("C")
        assert out.status == "completed" and sent == 1
    else:
        [d] = budget.open_discrepancies(s.conn, project["id"])
        assert (d["operator_micro"], d["reported_micro"], d["delta_micro"]) == (0, s.late_usage, s.late_usage)
        out, sent = s.worker("C")  # consolidate reused; extract would need new spend in a frozen budget
        assert out.status == "blocked" and out.blocked_reason.startswith("billing_discrepancy") and sent == 0
        assert s.run_state() == ("extract", "blocked")
        assert n(s.conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", s.rid) == 1
        budget.resolve_discrepancy(s.conn, d["id"], note="provider usage export now shows the call", actor="op")
        assert s.confirmed() == s.late_usage  # resolution acknowledges; it does not re-apply the difference
        unblock_run(s.conn, s.rid)
        out, sent = s.worker("D")
        assert out.status == "completed" and sent == 1
    assert len(s.prov.responses) == 1  # consolidate was sent exactly once in total
    assert s.attempts("consolidate")[-1][1] == "succeeded"
    assert n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE run_id = %s", s.rid) == 2
    expected_confirmed = s.late_usage + s.conn.execute(
        "SELECT actual_micro FROM reservations WHERE run_id = %s AND id <> %s", (s.rid, s.hold)).fetchone()["actual_micro"]
    assert s.confirmed() == expected_confirmed and budget.ledger_mismatches(s.conn, project["id"]) == []


# ---------------------------------------------------------------- resume BEFORE the response arrives
@pytest.mark.parametrize("amount", ["exact", "zero"])
def test_replacement_resuming_while_response_outstanding_does_not_resend(connect_db, artifacts, project, amount):
    s = _scenario(connect_db, artifacts, project)
    _reconcile(s, amount)
    unblock_run(s.conn, s.rid)
    # Billing is reconciled, but the request has neither completed nor been abandoned with evidence.
    out, sent = s.worker("C")
    assert out.status == "blocked" and out.blocked_reason.startswith("request_outstanding") and sent == 0
    assert s.run_state() == ("consolidate", "blocked")
    assert s.attempts("consolidate") == [("A", "abandoned"), ("C", "failed")]
    assert n(s.conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", s.rid) == 1  # no new hold

    s.deliver()
    assert s.receipt_for_a() is not None
    unblock_run(s.conn, s.rid)
    out, sent = s.worker("D")
    if amount == "zero":
        assert out.status == "blocked" and out.blocked_reason.startswith("billing_discrepancy") and sent == 0
        [d] = budget.open_discrepancies(s.conn, project["id"])
        budget.resolve_discrepancy(s.conn, d["id"], note="acknowledged", actor="op")
        unblock_run(s.conn, s.rid)
        out, sent = s.worker("E")
    assert out.status == "completed" and sent == 1
    assert len(s.prov.responses) == 1
    assert s.attempts("consolidate")[-1][1] == "succeeded"
    assert budget.ledger_mismatches(s.conn, project["id"]) == []


# ---------------------------------------------------------------- duplicate delivery of the same response
def test_duplicate_delivery_of_the_same_response_is_recorded_once(connect_db, artifacts, project):
    from somnia.errors import IntegrityFailure
    from somnia.gateway import record_response

    s = _scenario(connect_db, artifacts, project)
    _reconcile(s, "zero")
    s.deliver()
    first = s.receipt_for_a()
    snapshot = (s.confirmed(), n(s.conn, "SELECT count(*) AS n FROM billing_discrepancies WHERE reservation_id = %s",
                                 s.hold), budget.envelope_status(s.conn, s.run["envelope_id"]))
    again = record_response(s.conn, s.artifacts, s.hold, s.prov.responses[0])
    assert (again.provider_call_id, again.created, again.discrepancy_id is None) == (first["id"], False, True)
    assert (s.confirmed(), n(s.conn, "SELECT count(*) AS n FROM billing_discrepancies WHERE reservation_id = %s",
                             s.hold), budget.envelope_status(s.conn, s.run["envelope_id"])) == snapshot
    r = s.prov.responses[0]
    with pytest.raises(IntegrityFailure):  # a different body for the same request is refused, nothing changes
        record_response(s.conn, s.artifacts, s.hold, ProviderResponse(r.text + " ", r.input_tokens, r.output_tokens,
                                                                      r.request_id))
    assert n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE reservation_id = %s", s.hold) == 1
    assert s.confirmed() == snapshot[0] and budget.ledger_mismatches(s.conn, project["id"]) == []


# ---------------------------------------------------------------- leaf and parent totals; the whole tree is frozen
def test_discrepancy_is_accounted_once_in_leaf_and_parent_and_freezes_the_tree(connect_db, artifacts, project):
    from somnia.errors import BudgetFrozen

    conn, pid = connect_db(), project["id"]
    parent = budget.create_envelope(conn, pid, "phase", unit="MOCK_MICRO", limit_micro=1_000_000)
    leaf = budget.create_envelope(conn, pid, "run-leaf", unit="MOCK_MICRO", limit_micro=200_000, parent_id=parent)
    sibling = budget.create_envelope(conn, pid, "sibling", unit="MOCK_MICRO", limit_micro=100_000, parent_id=parent)
    session, _ = record_fixture_session(conn, pid, EXPERIENCE)
    snap = create_snapshot(conn, pid, [session]).snapshot_id
    rid = create_run(conn, project_id=pid, snapshot_id=snap, envelope_id=leaf, config=load_config(),
                     idempotency_key=f"tree-{uuid.uuid4().hex}")
    s = Scenario(connect_db, artifacts, {"run_id": rid, "envelope_id": leaf}, GatedProvider())
    _reconcile(s, "zero")
    s.deliver()
    assert (s.confirmed(leaf), s.confirmed(parent), s.confirmed(sibling)) == (s.late_usage, s.late_usage, 0)
    assert budget.ledger_mismatches(conn, pid) == []

    def reserve(env):
        return budget.reserve(conn, envelope_id=env, amount_micro=1_000, idempotency_key=uuid.uuid4().hex,
                              purpose="probe", provider="mock", model="m", pricing_snapshot={})
    for env in (leaf, sibling, parent):  # every envelope that draws on the affected parent is frozen
        with pytest.raises(BudgetFrozen):
            reserve(env)
    [d] = budget.open_discrepancies(conn, pid)
    budget.resolve_discrepancy(conn, d["id"], note="acknowledged", actor="op")
    held = reserve(sibling)
    budget.release_unsent(conn, held.id, note="probe never sent")
    assert (s.confirmed(leaf), s.confirmed(parent)) == (s.late_usage, s.late_usage)  # unchanged by resolution

    unblock_run(conn, rid)
    out, sent = s.worker("C")
    assert out.status == "completed" and sent == 1
    total = s.settled_total() + s.late_usage  # operator's 0 + the difference, once
    assert (s.confirmed(leaf), s.confirmed(parent), s.confirmed(sibling)) == (total, total, 0)
    assert budget.ledger_mismatches(conn, pid) == []


# ---------------------------------------------------------------- evidence above the limit
def test_evidence_above_the_limit_is_kept_reported_and_stops_spending(connect_db, artifacts, project):
    from somnia.errors import BudgetExceeded, BudgetFrozen
    from somnia.report import build_report

    # The provider reports far more usage than the reservation bound (real providers can).
    s = _scenario(connect_db, artifacts, project, provider=GatedProvider(usage_override=(150_000, 200)))
    _reconcile(s, "zero")
    s.deliver()
    env = budget.envelope_status(s.conn, s.run["envelope_id"])
    assert s.late_usage == 301_600 and env["confirmed_micro"] == s.late_usage > env["limit_micro"] == 200_000
    assert s.receipt_for_a()["input_tokens"] == 150_000  # evidence kept, not trimmed to fit the cap
    kinds = {a["kind"] for a in budget.alerts(s.conn, project["id"])}
    assert {"overrun", "open_discrepancy"} <= kinds
    assert {"overrun", "open_discrepancy"} <= {a["kind"] for a in build_report(s.conn, s.rid)["budget_alerts"]}
    with pytest.raises(BudgetFrozen):
        budget.reserve(s.conn, envelope_id=s.run["envelope_id"], amount_micro=1, idempotency_key=uuid.uuid4().hex,
                       purpose="probe", provider="mock", model="m", pricing_snapshot={})
    [d] = budget.open_discrepancies(s.conn, project["id"])
    budget.resolve_discrepancy(s.conn, d["id"], note="acknowledged overrun", actor="op")
    with pytest.raises(BudgetExceeded):  # resolved, but the evidence is above the limit: still no new spend
        budget.reserve(s.conn, envelope_id=s.run["envelope_id"], amount_micro=1, idempotency_key=uuid.uuid4().hex,
                       purpose="probe", provider="mock", model="m", pricing_snapshot={})
    assert {"overrun"} <= {a["kind"] for a in budget.alerts(s.conn, project["id"])}
    unblock_run(s.conn, s.rid)
    with pytest.raises(BudgetExceeded):
        s.worker("C")
    assert len(s.prov.responses) == 1 and budget.ledger_mismatches(s.conn, project["id"]) == []


# ---------------------------------------------------------------- a response that never arrives
def test_retry_after_no_response_needs_request_evidence_not_billing_reconciliation(connect_db, artifacts, project):
    from somnia.errors import InvalidRequestResolution

    conn = connect_db()
    run = make_run(conn, project["id"])
    rid = run["run_id"]

    def crash_after_reserve(name, info):
        if name == "gateway.after_reserve":
            raise SimulatedCrash(name)
    a = connect_db()
    with pytest.raises(SimulatedCrash):
        SleepWorker(a, artifacts, worker_id="A", lease_ttl_s=1, hook=crash_after_reserve).work(rid)
    a.close()
    hold = conn.execute("SELECT id FROM reservations WHERE run_id = %s", (rid,)).fetchone()["id"]
    with pytest.raises(InvalidRequestResolution):  # the hold is still 'reserved': its owner may be alive
        budget.abandon_request(conn, hold, evidence="provider log shows no such request", actor="op")
    wait_lease_expired(conn, rid)
    assert SleepWorker(connect_db(), artifacts, worker_id="B").work(rid).status == "blocked"

    budget.reconcile(conn, hold, actual_micro=0, note="provider usage export shows no usage for this window")
    unblock_run(conn, rid)
    prov_c = MockProvider(load_mock_script(SCRIPT))
    out = SleepWorker(connect_db(), artifacts, worker_id="C", provider=prov_c).work(rid)
    assert out.status == "blocked" and out.blocked_reason.startswith("request_outstanding") and prov_c.calls == []

    budget.abandon_request(conn, hold, actor="op", evidence="provider request log has no request with this"
                           " request key in the window; mock provider recorded no call (dev test)")
    unblock_run(conn, rid)
    prov_d = MockProvider(load_mock_script(SCRIPT))
    assert SleepWorker(connect_db(), artifacts, worker_id="D", provider=prov_d).work(rid).status == "completed"
    assert len(prov_d.calls) == 2  # consolidate retried (permitted by the abandonment evidence) + extract
    done = conn.execute("SELECT r.id FROM reservations r JOIN provider_calls c ON c.reservation_id = r.id"
                        " WHERE r.run_id = %s ORDER BY c.created_at LIMIT 1", (rid,)).fetchone()["id"]
    with pytest.raises(InvalidRequestResolution):  # a completed request cannot be abandoned
        budget.abandon_request(conn, done, evidence="provider log shows no such request", actor="op")
    assert budget.ledger_mismatches(conn, project["id"]) == []


def test_late_response_after_abandonment_is_still_recorded(connect_db, artifacts, project):
    s = _scenario(connect_db, artifacts, project)
    _reconcile(s, "zero")
    # A holds an open send permit, so abandonment needs a sender-stopped attestation. Here it is deliberately
    # WRONG (A is alive and its response is in flight) to show that evidence is still never discarded.
    budget.abandon_request(s.conn, s.hold, actor="op", evidence="provider status page reported the request failed",
                           sender_stopped_evidence="operator believed the worker host was rebooted (wrong)")
    unblock_run(s.conn, s.rid)
    out, sent = s.worker("C")  # the retry is permitted and sent
    assert out.status == "completed" and sent == 2
    s.deliver()  # ... and then the original response turns up after all
    assert s.receipt_for_a() is not None  # never discarded
    [d] = budget.open_discrepancies(s.conn, project["id"])
    assert d["delta_micro"] == s.late_usage and s.confirmed() == s.settled_total() + s.late_usage
    assert s.run_state() == ("done", "completed")  # A still cannot touch the finished run
    assert budget.ledger_mismatches(s.conn, project["id"]) == []

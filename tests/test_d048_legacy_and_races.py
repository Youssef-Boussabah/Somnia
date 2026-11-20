"""D048 (Codex review of the D044/D046 repair): legacy retry accounting, legacy late responses with an unknown
prompt, reconciliation racing abandonment, and provider errors after a takeover.

Mocks + disposable real PostgreSQL databases only. Nothing here touches the development database.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

import psycopg
import pytest

from conftest import EXPERIENCE, _with_db, load_config, make_run, wait_lease_expired
from somnia import budget, cli
from somnia.artifacts import ArtifactStore
from somnia.config import Settings
from somnia.db import connect
from somnia.engine import SimulatedCrash, SleepWorker, create_run, unblock_run
from somnia.errors import IntegrityFailure, InvalidRequestResolution, OperatorReceiptOnly, translating
from somnia.events import record_fixture_session
from somnia.gateway import CallContext, ModelGateway, record_response
from somnia.hashing import hash_obj, sha256_text
from somnia.interfaces import ProviderNotSent, ProviderOutcomeUnknown, ProviderResponse
from somnia.migrate import migrate
from somnia.mock import MockProvider, load_mock_script
from somnia.projects import create_project
from somnia.snapshots import create_snapshot
from test_gateway_recovery import SCRIPT, GatedProvider, Scenario, n

GW = load_config().gateway
BOUND = GW.max_input_tokens * GW.input_micro_per_token + GW.max_output_tokens * GW.output_micro_per_token  # 16,000
TEMPLATE, PROMPT = "consolidate.v2", "legacy consolidate prompt (synthetic, pre-0003)"


def usage_of(input_tokens: int, output_tokens: int) -> int:
    return input_tokens * GW.input_micro_per_token + output_tokens * GW.output_micro_per_token


RETRY_TOKENS, LATE_TOKENS = (300, 200), (400, 250)
RETRY_USAGE, LATE_USAGE = usage_of(*RETRY_TOKENS), usage_of(*LATE_TOKENS)  # 2,200 and 2,800


# =============================================================== legacy (pre-0003) databases
@pytest.fixture
def legacy_db():
    """A separate database at 0002, as the development database is today."""
    base = Settings.from_env().database_url
    name = f"somnia_d048_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = _with_db(base, name)
    conns: list[psycopg.Connection] = []

    def _open():
        c = connect(url)
        conns.append(c)
        return c
    assert migrate(_open(), target=2) == [1, 2]
    yield url, _open
    for c in conns:
        c.close()
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def legacy_key(run_id) -> str:
    """The request key the gateway derives for (run, consolidate, TEMPLATE, PROMPT) -- before and after 0003."""
    return hash_obj({"run": run_id, "stage": "consolidate", "template": TEMPLATE, "provider": "mock",
                     "model": GW.model, "prompt": sha256_text(PROMPT)})


def legacy_rows(conn, art, *, retried: bool, operator_micro: int) -> dict:
    """Rows as the pre-0003 code wrote them, on a 0002 schema: attempt 1 reserved and sent the request, lost its
    lease (hold marked uncertain) and was reconciled by an operator at ``operator_micro`` under pre-0003 rules.
    If ``retried``, the pre-0003 code then re-sent the same logical request, which completed (its own receipt)."""
    pid = create_project(conn, f"legacy-{uuid.uuid4().hex[:8]}")
    run = make_run(conn, pid)
    rid, key = run["run_id"], legacy_key(run["run_id"])
    atts = [conn.execute("INSERT INTO stage_attempts (run_id, stage, fence, worker_id, status, input_hash)"
                         " VALUES (%s, 'consolidate', %s, 'legacy', 'abandoned', 'x') RETURNING id",
                         (rid, fence)).fetchone()["id"] for fence in (1, 2)]

    def reserve(att):
        return budget.reserve(conn, envelope_id=run["envelope_id"], amount_micro=BOUND, idempotency_key=f"{key}:{att}",
                              purpose=f"consolidate:{TEMPLATE}", provider="mock", model=GW.model,
                              pricing_snapshot=GW.model_dump(mode="json"), run_id=rid, stage_attempt_id=att).id
    orphan = reserve(atts[0])
    budget.mark_uncertain(conn, orphan, note="legacy: worker lost its lease after sending")
    budget.reconcile(conn, orphan, actual_micro=operator_micro, note="legacy operator reconciliation")
    out = {"pid": pid, "run": run, "key": key, "orphan": orphan, "attempts": atts, "retry": None, "retry_call": None}
    if retried:
        retry = reserve(atts[1])
        budget.settle(conn, retry, actual_micro=RETRY_USAGE, request_id="legacy-retry-req")
        with conn.transaction():
            p = art.put(conn, PROMPT.encode("utf-8"), media_type="text/plain", origin="mock")
            r = art.put(conn, b'{"legacy": "retry response (synthetic)"}', media_type="application/json", origin="mock")
            out["retry_call"] = conn.execute(
                "INSERT INTO provider_calls (reservation_id, run_id, stage_attempt_id, request_key, provider, model,"
                " is_mock, prompt_artifact, response_artifact, input_tokens, output_tokens, request_id)"
                " VALUES (%s, %s, %s, %s, 'mock', %s, true, %s, %s, %s, %s, 'legacy-retry-req') RETURNING id",
                (retry, rid, atts[1], key, GW.model, p, r, *RETRY_TOKENS)).fetchone()["id"]
        out["retry"] = retry
    return out


def gateway_call(conn, art, legacy: dict, provider=None):
    """A post-upgrade gateway call for the same logical request (fresh stage attempt, no-op lease guard)."""
    att = conn.execute("INSERT INTO stage_attempts (run_id, stage, fence, worker_id, status, input_hash)"
                       " VALUES (%s, 'consolidate', 3, 'post-0003', 'running', 'x') RETURNING id",
                       (legacy["run"]["run_id"],)).fetchone()["id"]
    prov = provider or MockProvider(load_mock_script(SCRIPT))
    ctx = CallContext(run_id=legacy["run"]["run_id"], stage="consolidate", stage_attempt_id=att,
                      envelope_id=legacy["run"]["envelope_id"], guard=lambda c: None)
    return ModelGateway(conn, art, prov, GW).call(ctx, TEMPLATE, PROMPT), prov


def held_and_confirmed(conn, env):
    st = budget.envelope_status(conn, env)
    return st["reserved_micro"], st["confirmed_micro"]


# =============================================================== 1. legacy retry accounting
def test_legacy_retry_is_reused_but_the_original_hold_stays_until_its_own_evidence(legacy_db, tmp_path):
    _, open_db = legacy_db
    conn, art = open_db(), ArtifactStore(tmp_path / "artifacts")
    lg = legacy_rows(conn, art, retried=True, operator_micro=0)
    env = lg["run"]["envelope_id"]
    assert held_and_confirmed(conn, env) == (0, RETRY_USAGE)  # pre-0003: the original's hold was released at 0

    assert migrate(conn) == [3]
    # The migration does not supersede the original attempt: a retry receipt says nothing about its bill.
    assert n(conn, "SELECT count(*) AS n FROM provider_request_abandonments") == 0
    assert [r["reservation_id"] for r in budget.outstanding_requests(conn, lg["pid"])] == [lg["orphan"]]
    assert held_and_confirmed(conn, env) == (BOUND, RETRY_USAGE)  # its remaining possible charge is held again
    assert budget.ledger_mismatches(conn, lg["pid"]) == []

    # The retry's response stays reusable: a post-upgrade call of the same logical request reuses it,
    # with no reservation and no send, while the original's hold is untouched.
    holds_before = n(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", lg["run"]["run_id"])
    result, prov = gateway_call(conn, art, lg)
    assert (result.reused, result.provider_call_id, prov.calls) == (True, lg["retry_call"], [])
    assert n(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", lg["run"]["run_id"]) == holds_before
    assert held_and_confirmed(conn, env) == (BOUND, RETRY_USAGE)

    # Only the original's own evidence resolves it. Pre-0003 sender state is unknown, so abandonment needs both
    # provider-side evidence and a sender-stopped attestation.
    with pytest.raises(InvalidRequestResolution, match="sender"):
        budget.abandon_request(conn, lg["orphan"], actor="op", evidence="provider log: no request with this key")
    assert held_and_confirmed(conn, env) == (BOUND, RETRY_USAGE)
    budget.abandon_request(conn, lg["orphan"], actor="op", evidence="provider log: no request with this key",
                           sender_stopped_evidence="all pre-0003 workers were drained before the upgrade")
    assert held_and_confirmed(conn, env) == (0, RETRY_USAGE)
    assert budget.outstanding_requests(conn, lg["pid"]) == [] and budget.ledger_mismatches(conn, lg["pid"]) == []


# =============================================================== 2. legacy late response, prompt unknown
@pytest.mark.parametrize("retried", [False, True], ids=["never-retried", "retried"])
@pytest.mark.parametrize("estimate", ["zero", "exact"])
def test_legacy_late_response_is_recorded_once_with_its_prompt_unknown(legacy_db, tmp_path, monkeypatch, capsys,
                                                                       retried, estimate):
    url, open_db = legacy_db
    conn, art_dir = open_db(), tmp_path / "artifacts"
    art = ArtifactStore(art_dir)
    x = 0 if estimate == "zero" else LATE_USAGE
    lg = legacy_rows(conn, art, retried=retried, operator_micro=x)
    env, pid, orphan = lg["run"]["envelope_id"], lg["pid"], lg["orphan"]
    assert migrate(conn) == [3]
    retry_usage = RETRY_USAGE if retried else 0
    assert held_and_confirmed(conn, env) == (BOUND - x, retry_usage + x)  # residual = bound - operator figure

    late = ProviderResponse('{"legacy": "late response of attempt 1 (synthetic)"}', *LATE_TOKENS, "legacy-req-1")
    with pytest.raises(IntegrityFailure, match="record-response"):  # the gateway path cannot invent a prompt
        record_response(conn, art, orphan, late)
    with pytest.raises(InvalidRequestResolution, match="no recorded prompt"), translating():
        with conn.transaction():  # nor can hand-written SQL record a prompt-less call without an operator record
            r_sha = art.put(conn, late.text.encode("utf-8"), media_type="application/json", origin="mock")
            conn.execute("INSERT INTO provider_calls (reservation_id, run_id, stage_attempt_id, request_key, provider,"
                         " model, is_mock, prompt_artifact, response_artifact, input_tokens, output_tokens, request_id)"
                         " VALUES (%s, %s, %s, %s, 'mock', %s, true, NULL, %s, 1, 1, 'x')",
                         (orphan, lg["run"]["run_id"], lg["attempts"][0], lg["key"], GW.model, r_sha))
    with pytest.raises(InvalidRequestResolution, match="identity and prompt"), translating():
        with conn.transaction():  # ... or attach an invented prompt to a request whose prompt was never stored
            p_sha = art.put(conn, PROMPT.encode("utf-8"), media_type="text/plain", origin="mock")
            r_sha = art.put(conn, late.text.encode("utf-8"), media_type="application/json", origin="mock")
            conn.execute("INSERT INTO provider_calls (reservation_id, run_id, stage_attempt_id, request_key, provider,"
                         " model, is_mock, prompt_artifact, response_artifact, input_tokens, output_tokens, request_id)"
                         " VALUES (%s, %s, %s, %s, 'mock', %s, true, %s, %s, 1, 1, 'x')",
                         (orphan, lg["run"]["run_id"], lg["attempts"][0], lg["key"], GW.model, p_sha, r_sha))
    assert n(conn, "SELECT count(*) AS n FROM provider_calls WHERE reservation_id = %s", orphan) == 0

    # The supported operator path: `somnia budget record-response`, delivered twice.
    monkeypatch.setenv("SOMNIA_DATABASE_URL", url)
    monkeypatch.setenv("SOMNIA_ARTIFACT_DIR", str(art_dir))
    body = tmp_path / "late.json"
    body.write_text(late.text, encoding="utf-8")
    argv = ["budget", "record-response", str(orphan), "--response-file", str(body),
            "--input-tokens", str(LATE_TOKENS[0]), "--output-tokens", str(LATE_TOKENS[1]),
            "--request-id", late.request_id, "--actor", "op",
            "--evidence", "provider request log export for request legacy-req-1 (body and usage)"]
    receipts = []
    for _ in range(2):
        cli.main(argv)
        receipts.append(json.loads(capsys.readouterr().out))
    assert [r["created"] for r in receipts] == [True, False]
    assert receipts[0]["provider_call_id"] == receipts[1]["provider_call_id"]
    assert receipts[0]["reported_micro"] == LATE_USAGE and receipts[1]["discrepancy_id"] is None

    # Uncertainty preserved: no prompt is recorded or invented, and the record says who matched it, on what.
    call = conn.execute("SELECT c.prompt_artifact, o.prompt_recorded, o.actor, o.evidence FROM provider_calls c"
                        " JOIN operator_recorded_responses o ON o.provider_call_id = c.id"
                        " WHERE c.reservation_id = %s", (orphan,)).fetchone()
    assert (call["prompt_artifact"], call["prompt_recorded"], call["actor"]) == (None, False, "op")
    # Charged once, residual cleared.
    assert held_and_confirmed(conn, env) == (0, retry_usage + LATE_USAGE)
    resid = conn.execute("SELECT released_at IS NOT NULL AS released, release_reason FROM request_residual_holds"
                         " WHERE reservation_id = %s", (orphan,)).fetchone()
    assert (resid["released"], resid["release_reason"]) == (True, "response recorded")
    disc = budget.open_discrepancies(conn, pid)
    if estimate == "zero":
        assert [(d["operator_micro"], d["reported_micro"], d["delta_micro"]) for d in disc] == [(0, LATE_USAGE,
                                                                                                LATE_USAGE)]
        budget.resolve_discrepancy(conn, disc[0]["id"], note="acknowledged legacy late response", actor="op")
    else:
        assert disc == []
    assert budget.outstanding_requests(conn, pid) == [] and budget.ledger_mismatches(conn, pid) == []
    with pytest.raises(IntegrityFailure):  # a different body for the same request is still refused
        record_response(conn, art, orphan, ProviderResponse(late.text + " ", *LATE_TOKENS, late.request_id))
    assert held_and_confirmed(conn, env) == (0, retry_usage + LATE_USAGE)

    # The operator-recorded response is never reused as model output.
    holds = n(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", lg["run"]["run_id"])
    if retried:  # the gateway's own receipt from the retry is reused
        result, prov = gateway_call(conn, art, lg)
        assert (result.reused, result.provider_call_id, prov.calls) == (True, lg["retry_call"], [])
    else:  # D049: the only receipt is operator-recorded -> pause; no reservation, no send (was: a fresh send)
        prov = MockProvider(load_mock_script(SCRIPT))
        with pytest.raises(OperatorReceiptOnly, match="operator-recorded receipt"):
            gateway_call(conn, art, lg, provider=prov)
        assert prov.calls == []
    assert n(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", lg["run"]["run_id"]) == holds
    assert held_and_confirmed(conn, env) == (0, retry_usage + LATE_USAGE)
    assert budget.ledger_mismatches(conn, pid) == []


# =============================================================== 3. reconciliation racing abandonment
def _uncertain_hold_without_permit(connect_db, artifacts, project):
    """Run on leaf 'L' under parent 'P'; A crashes after reserving (no send permit, so nothing was sent);
    B marks the hold uncertain and blocks the run."""
    conn, pid = connect_db(), project["id"]
    parent = budget.create_envelope(conn, pid, "P", unit="MOCK_MICRO", limit_micro=100_000)
    leaf = budget.create_envelope(conn, pid, "L", unit="MOCK_MICRO", limit_micro=100_000, parent_id=parent)
    session, _ = record_fixture_session(conn, pid, EXPERIENCE)
    rid = create_run(conn, project_id=pid, snapshot_id=create_snapshot(conn, pid, [session]).snapshot_id,
                     envelope_id=leaf, config=load_config(), idempotency_key=f"race-{uuid.uuid4().hex}")

    def crash(name, info):
        if name == "gateway.after_reserve":
            raise SimulatedCrash(name)
    with pytest.raises(SimulatedCrash):
        SleepWorker(connect_db(), artifacts, worker_id="A", lease_ttl_s=1, hook=crash).work(rid)
    wait_lease_expired(conn, rid)
    assert SleepWorker(connect_db(), artifacts, worker_id="B").work(rid).status == "blocked"
    hold = conn.execute("SELECT id, state, amount_micro FROM reservations WHERE run_id = %s", (rid,)).fetchone()
    assert (hold["state"], hold["amount_micro"]) == ("uncertain", BOUND)
    return conn, rid, hold["id"], leaf, parent


def _waiting_on_lock(observer, pid) -> bool:
    return observer.execute("SELECT wait_event_type = 'Lock' AS w FROM pg_stat_activity WHERE pid = %s",
                            (pid,)).fetchone()["w"] is True


@pytest.mark.parametrize("via", ["api", "sql"])
@pytest.mark.parametrize("first", ["reconcile", "abandon"])
def test_reconciliation_and_abandonment_race_leaves_no_stranded_hold(connect_db, artifacts, project, first, via):
    """Two connections. The FIRST operation runs inside an open transaction (barrier: not yet committed) while
    the SECOND starts on another connection; then the first commits. Both orderings must end with the request
    abandoned, billing settled at the operator's figure, and nothing held on leaf or parent. ``via='sql'``
    reconciles with a plain UPDATE, so only the database's own guards serialize the two."""
    observer, rid, hold, leaf, parent = _uncertain_hold_without_permit(connect_db, artifacts, project)
    x = 4_000  # operator's billing figure; the residual it would create is BOUND - x = 12,000
    c1, c2 = connect_db(), connect_db()

    def do(op, c):
        if op == "reconcile" and via == "sql":
            with c.transaction():
                c.execute("UPDATE reservations SET state = 'settled', actual_micro = %s, resolution_note = %s"
                          " WHERE id = %s AND state = 'uncertain'", (x, "usage export: 4,000 (raw SQL)", hold))
        elif op == "reconcile":
            budget.reconcile(c, hold, actual_micro=x, note="usage export: 4,000 for this request window")
        else:
            budget.abandon_request(c, hold, actor="op", evidence="no send permit was ever granted for this request")
    second = "abandon" if first == "reconcile" else "reconcile"
    out: dict = {}

    def run_second():
        try:
            do(second, c2)
        except BaseException as exc:
            out["error"] = exc
    with c1.transaction():
        do(first, c1)  # done, not committed
        t = threading.Thread(target=run_second)
        t.start()
        deadline, waited = time.monotonic() + 10, False
        while time.monotonic() < deadline and t.is_alive():
            if _waiting_on_lock(observer, c2.info.backend_pid):
                waited = True
                break
            time.sleep(0.02)
    t.join(30)  # the first operation has committed
    assert not t.is_alive() and "error" not in out, out  # no deadlock, no refusal

    r = observer.execute("SELECT state, actual_micro FROM reservations WHERE id = %s", (hold,)).fetchone()
    assert (r["state"], r["actual_micro"]) == ("settled", x)
    assert n(observer, "SELECT count(*) AS n FROM provider_request_abandonments WHERE reservation_id = %s", hold) == 1
    assert n(observer, "SELECT count(*) AS n FROM request_residual_holds WHERE reservation_id = %s"
                       " AND released_at IS NULL", hold) == 0
    assert held_and_confirmed(observer, leaf) == held_and_confirmed(observer, parent) == (0, x)
    assert budget.outstanding_requests(observer, project["id"]) == []
    assert budget.ledger_mismatches(observer, project["id"]) == []
    assert waited, "the second operation did not wait for the first: they were not serialized"
    # The abandoned request may now be retried, once.
    unblock_run(observer, rid)
    prov = MockProvider(load_mock_script(SCRIPT))
    assert SleepWorker(connect_db(), artifacts, worker_id="C", provider=prov).work(rid).status == "completed"
    assert budget.ledger_mismatches(observer, project["id"]) == []


# =============================================================== 4. provider error after takeover
class ErrorAfterTakeover(GatedProvider):
    """The first call is accepted by the mock (A holds a send permit) but returns -- only once ``deliver`` is set,
    i.e. after the successor took over -- with a timeout or a definitive not-sent error instead of a response."""

    def __init__(self, error: str):
        super().__init__()
        self.error = error

    def complete(self, **kw):
        if self.calls:  # later calls behave normally
            return MockProvider.complete(self, **kw)
        self.calls.append(kw["request_key"])
        self.computed.set()
        assert self.deliver.wait(30)
        if self.error == "timeout":
            raise ProviderOutcomeUnknown("mock: read timeout after the request was written")
        raise ProviderNotSent("mock: 400 request rejected before acceptance")


def _billing_record(conn, hold, env):
    r = conn.execute("SELECT state, actual_micro, resolution_note FROM reservations WHERE id = %s", (hold,)).fetchone()
    trans = conn.execute("SELECT from_state, to_state, actual_micro FROM reservation_transitions"
                         " WHERE reservation_id = %s ORDER BY id", (hold,)).fetchall()
    resid = conn.execute("SELECT amount_micro, released_at FROM request_residual_holds WHERE reservation_id = %s",
                         (hold,)).fetchall()
    return dict(r), [dict(t) for t in trans], [dict(h) for h in resid], held_and_confirmed(conn, env)


@pytest.mark.parametrize("reconciled_first", [False, True], ids=["after-takeover", "after-reconciliation"])
@pytest.mark.parametrize("error", ["timeout", "not_sent"])
def test_provider_error_after_takeover_closes_permit_and_keeps_billing(connect_db, artifacts, project, error,
                                                                      reconciled_first):
    s = Scenario(connect_db, artifacts, make_run(connect_db(), project["id"]), ErrorAfterTakeover(error))
    env, key = s.run["envelope_id"], s.conn.execute("SELECT request_key FROM provider_requests WHERE reservation_id"
                                                    " = %s", (s.hold,)).fetchone()["request_key"]
    if reconciled_first:
        budget.reconcile(s.conn, s.hold, actual_micro=0, note="operator: no usage visible yet")
    permit = s.conn.execute("SELECT ended_at FROM provider_request_sends WHERE reservation_id = %s",
                            (s.hold,)).fetchone()
    assert permit["ended_at"] is None  # A is mid-call
    before = _billing_record(s.conn, s.hold, env)

    s.prov.deliver.set()  # A's call now returns its error
    s.thread.join(30)
    assert not s.thread.is_alive()
    assert type(s.out.get("a_err")).__name__ == "LeaseLost", s.out  # A cannot block or fail B's run

    # The permit is closed with the outcome; the billing record is exactly as the successor/operator left it.
    p = s.conn.execute("SELECT ended_at, outcome, end_note FROM provider_request_sends WHERE reservation_id = %s",
                       (s.hold,)).fetchone()
    expected = "outcome_unknown" if error == "timeout" else "not_sent"
    assert p["ended_at"] is not None and p["outcome"] == expected and "mock:" in p["end_note"]
    assert _billing_record(s.conn, s.hold, env) == before
    assert budget.ledger_mismatches(s.conn, project["id"]) == []

    # The error alone authorizes nothing: the request is still outstanding and no replacement sends it.
    if not reconciled_first:
        budget.reconcile(s.conn, s.hold, actual_micro=0, note="operator: provider usage shows nothing billed")
    [q] = budget.outstanding_requests(s.conn, project["id"])
    assert (q["reservation_id"], q["send_permit"], q["send_outcome"], q["residual_micro"]) == (s.hold, "ended",
                                                                                              expected, BOUND)
    unblock_run(s.conn, s.rid)
    out, sent = s.worker("C")
    assert out.status == "blocked" and out.blocked_reason.startswith("request_outstanding") and sent == 0
    assert held_and_confirmed(s.conn, env) == (BOUND, 0)

    # The sender is done (permit ended), so provider-side evidence suffices to abandon; then one retry.
    budget.abandon_request(s.conn, s.hold, actor="op",
                           evidence=f"provider request log: request {key[:16]} failed ({expected}); not billed")
    assert held_and_confirmed(s.conn, env) == (0, 0)
    unblock_run(s.conn, s.rid)
    prov_d = MockProvider(load_mock_script(SCRIPT))
    assert SleepWorker(connect_db(), artifacts, worker_id="D", provider=prov_d).work(s.rid).status == "completed"
    assert prov_d.calls.count(key) == 1 and s.prov.calls.count(key) == 1
    assert budget.ledger_mismatches(s.conn, project["id"]) == []

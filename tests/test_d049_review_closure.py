"""D049 (closing the Codex review of D048): an operator-recorded receipt pauses the run instead of being resent;
reported token counts are each nonnegative; operator-supplied response bytes are stored exactly.

Mocks + disposable real PostgreSQL databases only. Nothing here touches the development database.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from conftest import make_run
from somnia import budget, cli
from somnia.engine import SleepWorker, unblock_run
from somnia.gateway import OperatorRecord, record_response
from somnia.interfaces import ProviderResponse
from somnia.mock import MockProvider, load_mock_script
from somnia.report import build_report
from test_d048_legacy_and_races import BOUND, _uncertain_hold_without_permit, held_and_confirmed
from test_gateway_recovery import SCRIPT, GatedProvider, Scenario, n

EVIDENCE = "provider request log export: body and usage for this request id"


def resumes_pause(s: Scenario, times: int) -> None:
    """Resume the run ``times`` times; each must pause on the operator-only receipt with no reservation or send."""
    rid = s.rid
    before = (n(s.conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", rid),
              n(s.conn, "SELECT count(*) AS n FROM provider_requests WHERE run_id = %s", rid),
              n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE run_id = %s", rid),
              budget.envelope_status(s.conn, s.run["envelope_id"]))
    for i in range(times):
        unblock_run(s.conn, rid)
        prov = MockProvider(load_mock_script(SCRIPT))
        out = SleepWorker(s.connect_db(), s.artifacts, worker_id=f"R{i}", provider=prov).work(rid)
        assert out.status == "blocked" and out.blocked_reason.startswith("operator_recorded_response"), out
        assert prov.calls == []  # zero sends
        assert (n(s.conn, "SELECT count(*) AS n FROM reservations WHERE run_id = %s", rid),
                n(s.conn, "SELECT count(*) AS n FROM provider_requests WHERE run_id = %s", rid),
                n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE run_id = %s", rid),
                budget.envelope_status(s.conn, s.run["envelope_id"])) == before  # zero new reservations
    assert s.run_state() == ("consolidate", "blocked")
    rep = build_report(s.conn, rid)
    assert rep["status"] == "blocked" and rep["blocked_reason"].startswith("operator_recorded_response")


# =============================================================== 1. operator-only receipt pauses the run
def test_operator_only_receipt_pauses_every_resume_and_keeps_billing(connect_db, artifacts, project):
    """A's response reaches the operator (A is stuck in flight). It is recorded with the operator's evidence.
    Every later resume pauses visibly: no reservation, no send. A's eventual delivery of the same response
    returns the operator's receipt and changes nothing."""
    s = Scenario(connect_db, artifacts, make_run(connect_db(), project["id"]), GatedProvider())
    late = s.prov.responses[0]
    receipt = record_response(s.conn, s.artifacts, s.hold, late, operator=OperatorRecord("op", EVIDENCE))
    assert receipt.created and receipt.discrepancy_id is None
    assert held_and_confirmed(s.conn, s.run["envelope_id"]) == (0, s.late_usage)  # billing kept

    resumes_pause(s, 3)
    s.deliver()  # A's own delivery of the same response: the existing receipt, no conflict, no second charge
    assert n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE reservation_id = %s", s.hold) == 1
    assert n(s.conn, "SELECT count(*) AS n FROM operator_recorded_responses WHERE provider_call_id = %s",
             receipt.provider_call_id) == 1  # provenance kept
    assert held_and_confirmed(s.conn, s.run["envelope_id"]) == (0, s.late_usage)
    resumes_pause(s, 2)
    assert len(s.prov.responses) == 1  # the logical request was sent exactly once
    assert budget.open_discrepancies(s.conn, project["id"]) == []
    assert budget.ledger_mismatches(s.conn, project["id"]) == []


# =============================================================== 2. nonnegative token counts
def _state(conn, hold, leaf, parent, pid):
    r = conn.execute("SELECT state, actual_micro, request_id FROM reservations WHERE id = %s", (hold,)).fetchone()
    return {
        "hold": dict(r),
        "transitions": n(conn, "SELECT count(*) AS n FROM reservation_transitions WHERE reservation_id = %s", hold),
        "calls": n(conn, "SELECT count(*) AS n FROM provider_calls WHERE reservation_id = %s", hold),
        "operator_records": n(conn, "SELECT count(*) AS n FROM operator_recorded_responses WHERE reservation_id = %s",
                              hold),
        "artifacts": n(conn, "SELECT count(*) AS n FROM artifacts"),
        "residuals": [dict(x) for x in conn.execute(
            "SELECT amount_micro, released_at FROM request_residual_holds WHERE reservation_id = %s", (hold,))],
        "discrepancies": n(conn, "SELECT count(*) AS n FROM billing_discrepancies WHERE reservation_id = %s", hold),
        "leaf": held_and_confirmed(conn, leaf), "parent": held_and_confirmed(conn, parent),
        "outstanding": [q["reservation_id"] for q in budget.outstanding_requests(conn, pid)],
    }


# (input_tokens, output_tokens) at 2 / 8 MOCK_MICRO per token
NEGATIVE = {("input", "zero"): (-4, 1),       # -8 + 8 = 0
            ("input", "positive"): (-1, 1),   # -2 + 8 = 6
            ("output", "zero"): (4, -1),      # 8 - 8 = 0
            ("output", "positive"): (8, -1)}  # 16 - 8 = 8


@pytest.mark.parametrize("negative,total", list(NEGATIVE))
def test_negative_token_count_is_rejected_everywhere_and_changes_nothing(connect_db, artifacts, project,
                                                                        negative, total):
    conn, rid, hold, leaf, parent = _uncertain_hold_without_permit(connect_db, artifacts, project)
    budget.reconcile(conn, hold, actual_micro=0, note="operator: no usage visible yet")  # residual = BOUND
    tokens = NEGATIVE[(negative, total)]
    assert tokens[0] * 2 + tokens[1] * 8 >= 0  # the weighted total alone would not reveal the bad count
    before = _state(conn, hold, leaf, parent, project["id"])
    assert before["leaf"] == before["parent"] == (BOUND, 0) and before["outstanding"] == [hold]
    files_before = sorted(p.name for p in artifacts.root.rglob("*") if p.is_file())

    bad = ProviderResponse('{"bad": "usage"}', *tokens, "req-negative")
    for operator in (None, OperatorRecord("op", EVIDENCE)):  # gateway path and operator path share the boundary
        with pytest.raises(ValueError, match=f"{negative}_tokens must be a nonnegative"):
            record_response(conn, artifacts, hold, bad, operator=operator)
        assert _state(conn, hold, leaf, parent, project["id"]) == before
    assert sorted(p.name for p in artifacts.root.rglob("*") if p.is_file()) == files_before  # nothing stored

    prompt = conn.execute("SELECT prompt_artifact FROM provider_requests WHERE reservation_id = %s",
                          (hold,)).fetchone()["prompt_artifact"]
    q = conn.execute("SELECT run_id, stage_attempt_id, request_key, model FROM provider_requests WHERE"
                     " reservation_id = %s", (hold,)).fetchone()
    with pytest.raises(psycopg.errors.CheckViolation, match="provider_calls_tokens_nonnegative"):
        with conn.transaction():  # PostgreSQL rejects it too, for hand-written SQL
            r_sha = artifacts.put(conn, bad.text.encode("utf-8"), media_type="application/json", origin="mock")
            conn.execute("INSERT INTO provider_calls (reservation_id, run_id, stage_attempt_id, request_key, provider,"
                         " model, is_mock, prompt_artifact, response_artifact, input_tokens, output_tokens, request_id)"
                         " VALUES (%s, %s, %s, %s, 'mock', %s, true, %s, %s, %s, %s, 'req-negative')",
                         (hold, q["run_id"], q["stage_attempt_id"], q["request_key"], q["model"], prompt, r_sha,
                          *tokens))
    assert _state(conn, hold, leaf, parent, project["id"]) == before

    # Zero stays valid: a (0, 0) response settles at 0 against the operator's 0 and clears the residual.
    ok = record_response(conn, artifacts, hold, ProviderResponse('{"empty": true}', 0, 0, "req-zero"),
                         operator=OperatorRecord("op", EVIDENCE))
    assert ok.created and ok.reported_micro == 0 and ok.discrepancy_id is None
    after = _state(conn, hold, leaf, parent, project["id"])
    assert after["leaf"] == after["parent"] == (0, 0) and after["outstanding"] == []
    assert budget.ledger_mismatches(conn, project["id"]) == []


# =============================================================== 3. exact response bytes via --response-file
CRLF_BODY = 'línea 1: café\r\nligne 2 — fin\r\n\r\n{"k": "v"}\r\n'


def _cli_record(monkeypatch, capsys, db_url, artifacts, tmp_path, hold, data: bytes, tokens, request_id):
    monkeypatch.setenv("SOMNIA_DATABASE_URL", db_url)
    monkeypatch.setenv("SOMNIA_ARTIFACT_DIR", str(artifacts.root))
    f = tmp_path / "response.bin"
    f.write_bytes(data)
    cli.main(["budget", "record-response", str(hold), "--response-file", str(f), "--input-tokens", str(tokens[0]),
              "--output-tokens", str(tokens[1]), "--request-id", request_id, "--actor", "op", "--evidence", EVIDENCE])
    return json.loads(capsys.readouterr().out)


def _stored_response(conn, artifacts, call_id) -> bytes:
    sha = conn.execute("SELECT response_artifact FROM provider_calls WHERE id = %s", (call_id,)).fetchone()[
        "response_artifact"]
    return artifacts.get(sha)


def test_response_file_bytes_are_stored_exactly_and_gateway_redelivery_is_a_noop(
        connect_db, artifacts, project, db_url, monkeypatch, capsys, tmp_path):
    conn, rid, hold, leaf, parent = _uncertain_hold_without_permit(connect_db, artifacts, project)
    data = CRLF_BODY.encode("utf-8")
    assert b"\r\n" in data and len(data) != len(CRLF_BODY)  # CRLF and multi-byte UTF-8
    rec = _cli_record(monkeypatch, capsys, db_url, artifacts, tmp_path, hold, data, (40, 25), "req-crlf")
    assert rec["created"] is True
    assert _stored_response(conn, artifacts, rec["provider_call_id"]) == data  # byte-for-byte
    before = _state(conn, hold, leaf, parent, project["id"])
    assert before["leaf"] == (0, 40 * 2 + 25 * 8)

    # The same response through the gateway's recording path (no operator record): existing receipt, no
    # conflict, no second charge, no new evidence.
    again = record_response(conn, artifacts, hold, ProviderResponse(CRLF_BODY, 40, 25, "req-crlf"))
    assert (str(again.provider_call_id), again.created, again.discrepancy_id) == (rec["provider_call_id"], False, None)
    assert _state(conn, hold, leaf, parent, project["id"]) == before
    assert budget.ledger_mismatches(conn, project["id"]) == []


class CRLFGated(GatedProvider):
    """Like GatedProvider, but the (valid JSON) response text uses CRLF line breaks."""

    def complete(self, **kw):
        resp = MockProvider.complete(self, **kw)
        body = json.dumps(json.loads(resp.text), indent=1, ensure_ascii=False).replace("\n", "\r\n")
        resp = ProviderResponse(body, resp.input_tokens, resp.output_tokens, resp.request_id)
        self.responses.append(resp)
        if len(self.responses) == 1:
            self.computed.set()
            assert self.deliver.wait(30)
        return resp


def test_operator_crlf_file_then_original_sender_delivers_the_same_response(
        connect_db, artifacts, project, db_url, monkeypatch, capsys, tmp_path):
    """The in-flight sender's response (CRLF) reaches the operator first, via --response-file. When the sender's
    own delivery arrives through the gateway, it must match the stored bytes: no conflict, no second charge."""
    s = Scenario(connect_db, artifacts, make_run(connect_db(), project["id"]), CRLFGated())
    late = s.prov.responses[0]
    data = late.text.encode("utf-8")
    assert b"\r\n" in data
    rec = _cli_record(monkeypatch, capsys, db_url, artifacts, tmp_path, s.hold, data,
                      (late.input_tokens, late.output_tokens), late.request_id)
    assert _stored_response(s.conn, artifacts, rec["provider_call_id"]) == data
    confirmed = held_and_confirmed(s.conn, s.run["envelope_id"])
    assert confirmed == (0, s.late_usage)
    s.deliver()  # A: record_response returns the existing receipt; A then loses its lease as usual
    assert n(s.conn, "SELECT count(*) AS n FROM provider_calls WHERE reservation_id = %s", s.hold) == 1
    assert n(s.conn, "SELECT count(*) AS n FROM billing_discrepancies WHERE reservation_id = %s", s.hold) == 0
    assert held_and_confirmed(s.conn, s.run["envelope_id"]) == confirmed
    assert budget.ledger_mismatches(s.conn, project["id"]) == []

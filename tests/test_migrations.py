"""Upgrades of foundation-era databases (0001 -> 0002 -> 0003), on real PostgreSQL."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from conftest import _with_db, load_config, make_run
from somnia.artifacts import ArtifactStore
from somnia.config import Settings
from somnia.db import connect
from somnia.engine import SleepWorker
from somnia.errors import ActivationInvariantViolation, translating
from somnia.export_verify import verify_skill_set_export
from somnia.migrate import migrate, status
from somnia.projects import active_skill_set_id, create_project
from somnia.skills import export_skill_set, skill_set_export_digest


@pytest.fixture
def foundation_db():
    """A separate database migrated to 0001 only, as a foundation-era database would be."""
    base = Settings.from_env().database_url
    name = f"somnia_upg_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = _with_db(base, name)
    conns: list[psycopg.Connection] = []

    def _open():
        c = connect(url)
        conns.append(c)
        return c
    c = _open()
    assert migrate(c, target=1) == [1]
    yield _open
    for c in conns:
        c.close()
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _foundation_era_data(conn, art, pid, orphan):
    """Rows as the pre-0003 code wrote them, without today's gateway (which needs 0003): a mock-gated active
    skill set, one COMPLETED gateway call, and a second gateway hold with NO provider_calls row in state
      'uncertain' / 'reserved'  -- unresolved (0003 must refuse to install),
      'reconciled'              -- operator-reconciled at 0 under pre-0003 semantics, never retried,
      'superseded'              -- reconciled at 0, then retried: same logical request as the completed call."""
    from somnia import budget
    from somnia.activation import activate_candidate
    from somnia.contracts import GateVerdict, SkillBody
    from somnia.skills import create_skill_version, record_gate_decision

    base = active_skill_set_id(conn, pid)
    with conn.transaction():
        sv = create_skill_version(conn, project_id=pid, body=SkillBody(
            slug="legacy-skill", title="Legacy skill (synthetic)", procedure="synthetic procedure",
            applicability={"domain": "python-reliability"}, exclusions={}, known_limits="synthetic"),
            parent_version_id=None, origin="user", run_id=None)
    g = record_gate_decision(conn, skill_version_id=sv["skill_version_id"], baseline_skill_set_id=base, run_id=None,
                             verdict=GateVerdict(candidate_hash=sv["content_hash"], decision="pass", reason="mock",
                                                 validator="mock", validator_version="0", suite_id="s",
                                                 suite_hash="h", independent=False, is_mock=True))
    set_v1 = activate_candidate(conn, project_id=pid, gate_decision_id=g, expected_previous_set_id=base, actor="t",
                                idempotency_key=f"legacy-{uuid.uuid4().hex}", reason="t",
                                allow_mock_validation=True).new_set_id
    run = make_run(conn, pid)  # create_run only; no stage runs on the pre-0003 schema
    atts = [conn.execute("INSERT INTO stage_attempts (run_id, stage, fence, worker_id, status, input_hash)"
                         " VALUES (%s, 'consolidate', %s, 'legacy', 'abandoned', 'x') RETURNING id",
                         (run["run_id"], fence)).fetchone()["id"] for fence in (1, 2)]
    orphan_key = "a" * 64 if orphan == "superseded" else "b" * 64
    keys = {}
    for name, rk, att in (("orphan", orphan_key, atts[0]), ("completed", "a" * 64, atts[1])):
        keys[name] = budget.reserve(conn, envelope_id=run["envelope_id"], amount_micro=16_000,
                                    idempotency_key=f"{rk}:{att}", purpose="consolidate:consolidate.v2",
                                    provider="mock", model="mock-optimizer-fixture",
                                    pricing_snapshot={"input_micro_per_token": 2, "output_micro_per_token": 8},
                                    run_id=run["run_id"], stage_attempt_id=att).id
    budget.settle(conn, keys["completed"], actual_micro=2_318, request_id="legacy-req")
    with conn.transaction():
        p = art.put(conn, b"legacy prompt (synthetic)", media_type="text/plain", origin="mock")
        r = art.put(conn, b"{}", media_type="application/json", origin="mock")
        conn.execute("INSERT INTO provider_calls (reservation_id, run_id, stage_attempt_id, request_key, provider, model,"
                     " is_mock, prompt_artifact, response_artifact, input_tokens, output_tokens, request_id)"
                     " VALUES (%s, %s, %s, %s, 'mock', 'mock-optimizer-fixture', true, %s, %s, 10, 10, 'legacy-req')",
                     (keys["completed"], run["run_id"], atts[1], "a" * 64, p, r))
    if orphan != "reserved":
        budget.mark_uncertain(conn, keys["orphan"], note="legacy: worker died after reserve")
    if orphan in ("reconciled", "superseded"):
        budget.reconcile(conn, keys["orphan"], actual_micro=0, note="legacy operator reconciliation at 0")
    return set_v1, run, keys


def test_upgrade_keeps_foundation_data_and_enforces_new_invariants(foundation_db, tmp_path):
    from somnia import budget

    conn = foundation_db()
    art = ArtifactStore(tmp_path / "artifacts")
    pid = create_project(conn, "upgrade-a")
    set_v1, legacy_run, keys = _foundation_era_data(conn, art, pid, orphan="superseded")  # written on 0001
    digest_before = skill_set_export_digest(export_skill_set(conn, set_v1))
    env_before = budget.envelope_status(conn, legacy_run["envelope_id"])

    assert migrate(conn) == [2, 3]
    assert migrate(conn) == []
    assert [(s["version"], s["state"]) for s in status(conn)] == [(1, "ok"), (2, "ok"), (3, "ok")]

    # Existing records still export identically and verify offline.
    exp = export_skill_set(conn, set_v1)
    assert skill_set_export_digest(exp) == digest_before
    verify_skill_set_export(exp.model_dump_json(), digest_before)
    # 0003 backfill: both legacy gateway holds got a request identity; the completed call keeps its prompt.
    q = {r["reservation_id"]: r for r in conn.execute("SELECT reservation_id, request_key, prompt_artifact"
                                                       " FROM provider_requests").fetchall()}
    assert set(q) == set(keys.values())
    assert q[keys["completed"]]["request_key"] == "a" * 64 and q[keys["completed"]]["prompt_artifact"] is not None
    # The reconciled hold whose logical request later completed under the retry is NOT resolved by the
    # migration (D048): the retry's receipt says nothing about what the first attempt billed. It stays
    # outstanding with its remaining possible charge held (bound 16,000 - operator's 0).
    assert conn.execute("SELECT count(*) AS n FROM provider_request_abandonments").fetchone()["n"] == 0
    assert [r["reservation_id"] for r in budget.outstanding_requests(conn, pid)] == [keys["orphan"]]
    env_after = budget.envelope_status(conn, legacy_run["envelope_id"])
    assert env_after["confirmed_micro"] == env_before["confirmed_micro"]
    assert env_after["reserved_micro"] == env_before["reserved_micro"] + 16_000
    assert budget.ledger_mismatches(conn, pid) == []
    # A new cycle completes on the upgraded database.
    run2 = make_run(conn, pid, envelope="mock-dev-2")
    assert SleepWorker(conn, art, worker_id="w2").work(run2["run_id"]).status == "completed"
    assert export_skill_set(conn, active_skill_set_id(conn, pid)).version == 2

    # The new triggers reject cross-project membership and pointers, even from hand-written SQL.
    other = create_project(conn, "upgrade-b")
    other_set = active_skill_set_id(conn, other)
    m = conn.execute("SELECT skill_id, skill_version_id, gate_decision_id FROM skill_set_members"
                     " WHERE skill_set_id = %s", (set_v1,)).fetchone()
    with pytest.raises(ActivationInvariantViolation), translating():
        conn.execute("INSERT INTO skill_set_members VALUES (%s, %s, %s, %s)",
                     (other_set, m["skill_id"], m["skill_version_id"], m["gate_decision_id"]))
    with pytest.raises(ActivationInvariantViolation), translating():
        conn.execute("UPDATE active_skill_sets SET skill_set_id = %s WHERE project_id = %s", (set_v1, other))
    assert active_skill_set_id(conn, other) == other_set


def test_upgrade_refuses_foundation_data_that_crosses_projects(foundation_db):
    conn = foundation_db()
    a, b = create_project(conn, "cross-a"), create_project(conn, "cross-b")
    # Hand-made 0001-era corruption: a gate-passed skill of project A listed in project B's set.
    from somnia.contracts import SkillBody
    from somnia.skills import create_skill_version
    with conn.transaction():
        sv = create_skill_version(conn, project_id=a, body=SkillBody(
            slug="x", title="t", procedure="p", applicability={"domain": "d"}, exclusions={}, known_limits="k"),
            parent_version_id=None, origin="user", run_id=None)
    g = conn.execute(
        "INSERT INTO gate_decisions (skill_version_id, candidate_hash, baseline_skill_set_id, validator,"
        " validator_version, suite_id, suite_hash, independent, is_mock, decision, reason, paired_scores,"
        " evidence_refs) VALUES (%s, %s, %s, 'x', '1', 's', 'h', false, true, 'pass', 'r', '[]', '[]') RETURNING id",
        (sv["skill_version_id"], sv["content_hash"], active_skill_set_id(conn, a))).fetchone()["id"]
    conn.execute("INSERT INTO skill_set_members VALUES (%s, %s, %s, %s)",
                 (active_skill_set_id(conn, b), sv["skill_id"], sv["skill_version_id"], g))
    with pytest.raises(ActivationInvariantViolation, match="cross a project boundary"), translating():
        migrate(conn)
    assert [(s["version"], s["state"]) for s in status(conn)] == [(1, "ok"), (2, "pending"), (3, "pending")]


@pytest.mark.parametrize("orphan", ["uncertain", "reserved"])
def test_upgrade_to_0003_stops_safely_on_unresolved_legacy_gateway_hold(foundation_db, tmp_path, orphan):
    """0002 -> 0003 with a gateway hold that has no provider_calls row and is still 'reserved'/'uncertain':
    its pre-0003 sender may be alive. The migration refuses before installing anything, with instructions;
    following them (drain, mark, reconcile) lets it install, and the legacy request stays outstanding with
    its remaining charge covered until resolved with evidence."""
    from somnia import budget
    from somnia.errors import InvalidRequestResolution

    conn = foundation_db()
    assert migrate(conn, target=2) == [2]
    art = ArtifactStore(tmp_path / "artifacts")
    pid = create_project(conn, "upgrade-unresolved")
    _, run, keys = _foundation_era_data(conn, art, pid, orphan=orphan)  # written on 0002
    env = run["envelope_id"]
    before = budget.envelope_status(conn, env)

    with pytest.raises(InvalidRequestResolution) as exc, translating():
        migrate(conn)
    msg = str(exc.value)
    for instruction in ("stop every", "somnia budget reconcile", str(keys["orphan"]), "somnia db migrate"):
        assert instruction in msg, msg
    assert [(s["version"], s["state"]) for s in status(conn)] == [(1, "ok"), (2, "ok"), (3, "pending")]
    assert conn.execute("SELECT to_regclass('somnia.provider_requests') AS t").fetchone()["t"] is None
    assert budget.envelope_status(conn, env) == before  # nothing changed

    # Follow the instructions on the pre-0003 schema: (a reserved hold is marked uncertain by the next
    # claim once its lease expired -- done directly here), then reconcile billing from evidence.
    if orphan == "reserved":
        budget.mark_uncertain(conn, keys["orphan"], note="claimed after lease expiry (pre-0003 drain)")
    budget.reconcile(conn, keys["orphan"], actual_micro=0, note="usage export shows no usage for this window")
    assert migrate(conn) == [3]

    # Recovery path on 0003: the request is outstanding (no resend) and its remaining charge stays covered.
    assert [r["reservation_id"] for r in budget.outstanding_requests(conn, pid)] == [keys["orphan"]]
    st = budget.envelope_status(conn, env)
    assert (st["reserved_micro"], st["confirmed_micro"]) == (16_000, 2_318)  # residual hold = bound - 0
    assert budget.ledger_mismatches(conn, pid) == []
    with pytest.raises(InvalidRequestResolution, match="sender"):  # pre-0003 sender state is unknown
        budget.abandon_request(conn, keys["orphan"], actor="op", evidence="provider log shows no such request")
    budget.abandon_request(conn, keys["orphan"], actor="op", evidence="provider log shows no such request",
                           sender_stopped_evidence="all pre-0003 workers stopped before the migration (drained)")
    st = budget.envelope_status(conn, env)
    assert (st["reserved_micro"], st["confirmed_micro"]) == (0, 2_318)
    assert budget.outstanding_requests(conn, pid) == [] and budget.ledger_mismatches(conn, pid) == []


def test_upgrade_to_0003_keeps_cover_for_a_reconciled_legacy_request(foundation_db, tmp_path):
    """A hold reconciled at 0 under pre-0003 semantics and never retried: still outstanding after upgrade,
    with its remaining possible charge held (not released by the migration)."""
    from somnia import budget

    conn = foundation_db()
    assert migrate(conn, target=2) == [2]
    art = ArtifactStore(tmp_path / "artifacts")
    pid = create_project(conn, "upgrade-reconciled")
    _, run, keys = _foundation_era_data(conn, art, pid, orphan="reconciled")
    assert migrate(conn) == [3]
    assert [r["reservation_id"] for r in budget.outstanding_requests(conn, pid)] == [keys["orphan"]]
    st = budget.envelope_status(conn, run["envelope_id"])
    assert (st["reserved_micro"], st["confirmed_micro"]) == (16_000, 2_318)
    assert budget.ledger_mismatches(conn, pid) == []

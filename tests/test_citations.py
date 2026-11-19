"""Experience citations stay unambiguous when sessions in one snapshot reuse idempotency keys.

Event idempotency keys are unique only within a session. Consolidation must cite events with a
snapshot-local reference that names the session, and persist the resolved event id."""

from __future__ import annotations

import copy
import json

import pytest

from conftest import EXPERIENCE, make_run
from somnia.engine import SleepWorker
from somnia.errors import FixtureError
from somnia.events import open_session, record_event, record_fixture_session
from somnia.mock import MockProvider, load_mock_script
from somnia.report import trace_skill_version
from somnia.snapshots import create_snapshot

SKILL = "durable-output-before-progress"


def _snapshot_with_key_reuse(conn, pid):
    """Session 1 is the fixture; session 2 reuses evt-001..evt-005 for different (synthetic) observations."""
    s1, r1 = record_fixture_session(conn, pid, EXPERIENCE)
    s2 = open_session(conn, pid, partition="development", origin="synthetic", purpose="experience",
                      agent_config_hash="second-session", label="second session reusing keys (synthetic)")
    r2 = [record_event(conn, session_id=s2, seq=i, event_type="observation",
                       payload={"text": f"SYNTHETIC second-session observation {i}"}, idempotency_key=f"evt-{i:03d}")
          for i in range(1, 6)]
    snap = create_snapshot(conn, pid, [s1, s2]).snapshot_id
    ids = {(1, f"evt-{i:03d}"): r.event_id for i, r in enumerate(r1, 1)}
    ids |= {(2, f"evt-{i:03d}"): r.event_id for i, r in enumerate(r2, 1)}
    return snap, ids


def _sources(conn, run_id):
    out: dict[int, set] = {}
    for r in conn.execute("SELECT h.ordinal, hs.event_id FROM hypotheses h JOIN hypothesis_sources hs"
                          " ON hs.hypothesis_id = h.id WHERE h.run_id = %s", (run_id,)).fetchall():
        out.setdefault(r["ordinal"], set()).add(r["event_id"])
    return out


def _skill_evidence_events(conn, run_id, slug=SKILL):
    rows = conn.execute("SELECT e.event_id FROM skill_version_evidence e JOIN skill_versions v ON v.id = e.skill_version_id"
                        " JOIN skills s ON s.id = v.skill_id WHERE v.created_by_run_id = %s AND s.slug = %s"
                        " AND e.event_id IS NOT NULL", (run_id, slug)).fetchall()
    return {r["event_id"] for r in rows}


def _version_id(conn, run_id, slug=SKILL):
    return conn.execute("SELECT v.id FROM skill_versions v JOIN skills s ON s.id = v.skill_id"
                        " WHERE v.created_by_run_id = %s AND s.slug = %s", (run_id, slug)).fetchone()["id"]


def test_fixture_citations_resolve_to_the_cited_session(conn, artifacts, project):
    snap, ids = _snapshot_with_key_reuse(conn, project["id"])
    run = make_run(conn, project["id"], snapshot_id=snap)
    assert SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"]).status == "completed"
    # The fixture's hypotheses are about session 1; session 2's same-key events must not be cited.
    assert _sources(conn, run["run_id"]) == {1: {ids[(1, "evt-002")], ids[(1, "evt-004")]},
                                             2: {ids[(1, "evt-002")]}}
    assert _skill_evidence_events(conn, run["run_id"]) == {ids[(1, "evt-002")], ids[(1, "evt-004")]}
    t = trace_skill_version(conn, artifacts, _version_id(conn, run["run_id"]))
    assert t["ok"], t["problems"]


def _variant(mutate):
    script = copy.deepcopy(load_mock_script("indexer_checkpoint_v1"))
    mutate(script)
    return script


def test_citations_into_both_sessions_resolve_from_prompt_to_evidence(conn, artifacts, project):
    snap, ids = _snapshot_with_key_reuse(conn, project["id"])

    def mutate(s):
        hyps = s["responses"]["consolidate.v2"]["hypotheses"]
        hyps[0]["source_event_refs"] = ["s1/evt-002"]
        hyps[1]["source_event_refs"] = ["s2/evt-002"]
        s["responses"]["extract.v1"]["candidates"][0]["evidence"]["hypothesis_ordinals"] = [2]

    run = make_run(conn, project["id"], snapshot_id=snap)
    w = SleepWorker(conn, artifacts, worker_id="w", provider=MockProvider(_variant(mutate)))
    assert w.work(run["run_id"]).status == "completed"
    assert _sources(conn, run["run_id"]) == {1: {ids[(1, "evt-002")]}, 2: {ids[(2, "evt-002")]}}

    # The prompt the model received distinguishes the two same-key events.
    sha = conn.execute("SELECT prompt_artifact FROM provider_calls WHERE run_id = %s ORDER BY created_at LIMIT 1",
                       (run["run_id"],)).fetchone()["prompt_artifact"]
    header, body = artifacts.get(sha).decode("utf-8").split("\n", 1)
    assert header == "TEMPLATE consolidate.v2"
    entries = {e["ref"]: e for e in json.loads(body)}
    assert len(entries) == 10
    assert entries["s1/evt-002"]["payload"]["tool"] == "write_file"
    assert entries["s2/evt-002"]["payload"] == {"text": "SYNTHETIC second-session observation 2"}

    # Candidate evidence follows hypothesis 2 into session 2, and the trace verifies.
    assert _skill_evidence_events(conn, run["run_id"]) == {ids[(2, "evt-002")]}
    t = trace_skill_version(conn, artifacts, _version_id(conn, run["run_id"]))
    assert t["ok"], t["problems"]


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda s: s["responses"]["consolidate.v2"]["hypotheses"][0].update(source_event_refs=["evt-002"]),
                 id="bare-key"),
    pytest.param(lambda s: s["responses"]["consolidate.v2"]["hypotheses"][0].update(source_event_refs=["s3/evt-002"]),
                 id="unknown-session"),
    pytest.param(lambda s: s["responses"]["consolidate.v2"]["hypotheses"][0].update(
        source_event_keys=s["responses"]["consolidate.v2"]["hypotheses"][0].pop("source_event_refs")),
                 id="legacy-keys-field"),
])
def test_ambiguous_or_unknown_citation_is_rejected(conn, artifacts, project, mutate):
    snap, _ = _snapshot_with_key_reuse(conn, project["id"])
    run = make_run(conn, project["id"], snapshot_id=snap)
    with pytest.raises(FixtureError):
        SleepWorker(conn, artifacts, worker_id="w", provider=MockProvider(_variant(mutate))).work(run["run_id"])
    assert conn.execute("SELECT count(*) AS n FROM hypotheses WHERE run_id = %s",
                        (run["run_id"],)).fetchone()["n"] == 0

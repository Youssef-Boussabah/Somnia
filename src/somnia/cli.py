"""Somnia command-line interface (development / mock mode)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from . import budget
from .artifacts import ArtifactStore
from .config import Settings
from .contracts import EXPORTED_MODELS, RetrievalRequest, RunConfig, TaskScope
from .db import connect
from .engine import SleepWorker, create_run, unblock_run
from .events import open_session, record_fixture_session
from .migrate import migrate, status
from .projects import active_skill_set_id, create_project, project_id
from .report import build_report, render_text, trace_skill_version
from .retrieval import render_context, retrieve_context
from .export_verify import ExportVerificationError, scientific_use_blockers, verify_skill_set_export
from .skills import export_skill_set, skill_set_export_digest
from .snapshots import create_snapshot

FIXTURES = Path("fixtures/development")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _ctx():
    s = Settings.from_env()
    return connect(s.database_url), ArtifactStore(s.artifact_dir)


def cmd_db(a):
    conn, _ = _ctx()
    if a.action == "migrate":
        _print({"applied": migrate(conn)})
    _print(status(conn))


def cmd_project_init(a):
    conn, _ = _ctx()
    _print({"project_id": create_project(conn, a.slug)})


def cmd_experience_record(a):
    conn, _ = _ctx()
    sid, receipts = record_fixture_session(conn, project_id(conn, a.project), Path(a.fixture))
    _print({"session_id": sid, "events": [r.model_dump(mode="json") for r in receipts]})


def cmd_snapshot_create(a):
    conn, _ = _ctx()
    info = create_snapshot(conn, project_id(conn, a.project), [uuid.UUID(s) for s in a.session])
    _print(info.model_dump(mode="json"))


def cmd_budget_envelope(a):
    conn, _ = _ctx()
    pid = project_id(conn, a.project)
    parent = budget.envelope_by_name(conn, pid, a.parent) if a.parent else None
    _print({"envelope_id": budget.create_envelope(conn, pid, a.name, unit=a.unit, limit_micro=a.limit_micro,
                                                  parent_id=parent)})


def cmd_budget_show(a):
    conn, _ = _ctx()
    pid = project_id(conn, a.project)
    envs = conn.execute("SELECT id FROM budget_envelopes WHERE project_id = %s ORDER BY created_at", (pid,)).fetchall()
    _print({"envelopes": [budget.envelope_status(conn, e["id"]) for e in envs],
            "summary_by_unit": budget.spend_summary(conn, pid), "outstanding": budget.outstanding(conn, pid),
            "alerts": budget.alerts(conn, pid), "ledger_mismatches": budget.ledger_mismatches(conn, pid)})


def cmd_budget_reconcile(a):
    """Billing only: settles what an uncertain hold cost. It does not permit resending the request."""
    conn, _ = _ctx()
    budget.reconcile(conn, uuid.UUID(a.reservation), actual_micro=a.actual_micro, note=a.note)
    _print({"reconciled": a.reservation,
            "note": "billing reconciled; the request stays outstanding until its response is recorded or it is"
                    " abandoned with provider-side evidence (budget abandon-request)"})


def cmd_budget_abandon(a):
    conn, _ = _ctx()
    budget.abandon_request(conn, uuid.UUID(a.reservation), evidence=a.evidence, actor=a.actor,
                           sender_stopped_evidence=a.sender_stopped)
    _print({"abandoned_request": a.reservation})


def cmd_budget_record_response(a):
    """Record a genuine late response that reached the operator, not the gateway (e.g. a pre-0003 request).
    Settles or reconciles billing once; a duplicate delivery changes nothing. The prompt is never invented."""
    from .gateway import OperatorRecord, record_response
    from .interfaces import ProviderResponse

    conn, art = _ctx()
    # Exact bytes, strict UTF-8: read_text() would translate CRLF to LF and change the response (D049).
    text = Path(a.response_file).read_bytes().decode("utf-8")
    receipt = record_response(conn, art, uuid.UUID(a.reservation),
                              ProviderResponse(text, a.input_tokens, a.output_tokens, a.request_id),
                              operator=OperatorRecord(actor=a.actor, evidence=a.evidence))
    _print({"provider_call_id": receipt.provider_call_id, "created": receipt.created,
            "reported_micro": receipt.reported_micro, "discrepancy_id": receipt.discrepancy_id})


def cmd_budget_resolve(a):
    conn, _ = _ctx()
    budget.resolve_discrepancy(conn, uuid.UUID(a.discrepancy), note=a.note, actor=a.actor)
    _print({"resolved_discrepancy": a.discrepancy})


def cmd_run_create(a):
    conn, _ = _ctx()
    pid = project_id(conn, a.project)
    cfg = RunConfig.model_validate_json(Path(a.config).read_text(encoding="utf-8"))
    rid = create_run(conn, project_id=pid, snapshot_id=uuid.UUID(a.snapshot),
                     envelope_id=budget.envelope_by_name(conn, pid, a.envelope), config=cfg, idempotency_key=a.key)
    _print({"run_id": rid})


def cmd_run_work(a):
    conn, art = _ctx()
    w = SleepWorker(conn, art, worker_id=a.worker_id or f"cli-{uuid.uuid4().hex[:8]}", lease_ttl_s=a.lease_ttl)
    out = w.work(uuid.UUID(a.run_id))
    _print(out.__dict__)


def cmd_run_unblock(a):
    conn, _ = _ctx()
    unblock_run(conn, uuid.UUID(a.run_id))
    _print({"unblocked": a.run_id})


def cmd_report(a):
    conn, _ = _ctx()
    r = build_report(conn, uuid.UUID(a.run_id))
    print(json.dumps(r, indent=2, default=str) if a.json else render_text(r))


def cmd_trace(a):
    conn, art = _ctx()
    t = trace_skill_version(conn, art, uuid.UUID(a.skill_version_id))
    _print(t)
    if not t["ok"]:
        sys.exit(1)


def cmd_retrieve(a):
    conn, _ = _ctx()
    pid = project_id(conn, a.project)
    sid = open_session(conn, pid, partition="development", origin="mock", purpose="retrieval",
                       agent_config_hash="cli-retrieval", label="fresh retrieval session (cli)")
    res = retrieve_context(conn, RetrievalRequest(project_slug=a.project, session_id=sid, task_query=a.query,
                                                  scope=TaskScope(domain=a.domain, tags=a.tags or [])))
    if a.json:
        _print(res.model_dump(mode="json"))
    else:
        print(render_context(res))
        for e in res.excluded:
            print(f"(excluded {e.skill_slug}: {e.reason} - {e.detail})")


def cmd_skillset_export(a):
    """Print (or write) a SkillSetExport v2. The export digest goes to stderr (or stdout with --out):
    hand it to the evaluator through a trusted channel, separately from the JSON."""
    conn, _ = _ctx()
    pid = project_id(conn, a.project)
    sid = uuid.UUID(a.set) if a.set else active_skill_set_id(conn, pid)
    exp = export_skill_set(conn, sid)
    text = json.dumps(exp.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    digest = f"export_digest sha256:{skill_set_export_digest(exp)}"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
        print(digest)
    else:
        sys.stdout.write(text)
        print(digest, file=sys.stderr)


def cmd_skillset_verify(a):
    """Verify an exported JSON file against a trusted digest. Uses no database."""
    try:
        doc = verify_skill_set_export(Path(a.file).read_bytes(), a.digest)
    except ExportVerificationError as exc:
        _print({"ok": False, "problems": exc.problems})
        sys.exit(1)
    _print({"ok": True, "skill_set_id": doc["skill_set_id"], "version": doc["version"],
            "members": len(doc["members"]), "scientific_use_blockers": scientific_use_blockers(doc)})


def cmd_contracts_export(a):
    out = Path(a.dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, model in EXPORTED_MODELS.items():
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
                                                 encoding="utf-8", newline="\n")
    print(f"wrote {len(EXPORTED_MODELS)} schemas to {out}")


def cmd_demo(a):
    """End-to-end mock learning cycle on a fresh project; prints each step's real output."""
    conn, art = _ctx()
    migrate(conn)
    slug = a.slug or f"demo-{uuid.uuid4().hex[:8]}"
    pid = create_project(conn, slug)
    sid, receipts = record_fixture_session(conn, pid, FIXTURES / "experience_indexer_checkpoint.json")
    snap = create_snapshot(conn, pid, [sid])
    env = budget.create_envelope(conn, pid, "mock-dev", unit="MOCK_MICRO", limit_micro=200_000)
    cfg = RunConfig.model_validate_json((FIXTURES / "run_config.mock.json").read_text(encoding="utf-8"))
    rid = create_run(conn, project_id=pid, snapshot_id=snap.snapshot_id, envelope_id=env, config=cfg,
                     idempotency_key=f"{slug}:run1")
    out = SleepWorker(conn, art, worker_id="demo-worker").work(rid)
    print(f"project={slug} session={sid} events={len(receipts)} snapshot={snap.snapshot_id} run={rid} "
          f"status={out.status} stages={out.stages_run}\n")
    print(render_text(build_report(conn, rid)))
    conn.close()
    # A fresh connection and a new session, as a later agent invocation would have.
    conn2, _ = _ctx()
    s2 = open_session(conn2, pid, partition="development", origin="mock", purpose="retrieval",
                      agent_config_hash="demo-fresh-session", label="fresh session after sleep")
    res = retrieve_context(conn2, RetrievalRequest(
        project_slug=slug, session_id=s2, task_query="resumable job that writes results and a progress checkpoint",
        scope=TaskScope(domain="python-reliability", tags=["resumable", "local-persistence"])))
    print("\n--- fresh-session retrieval ---")
    print(render_context(res))
    print(f"(retrieval_log={res.retrieval_log_id}, set v{res.skill_set_version}, tokens={res.token_count})")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="somnia", description="Somnia foundation CLI (mock mode only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("db"); d.add_argument("action", choices=["migrate", "status"]); d.set_defaults(fn=cmd_db)
    pr = sub.add_parser("project").add_subparsers(dest="sub", required=True)
    x = pr.add_parser("init"); x.add_argument("slug"); x.set_defaults(fn=cmd_project_init)
    ex = sub.add_parser("experience").add_subparsers(dest="sub", required=True)
    x = ex.add_parser("record"); x.add_argument("--project", required=True); x.add_argument("--fixture", required=True)
    x.set_defaults(fn=cmd_experience_record)
    sn = sub.add_parser("snapshot").add_subparsers(dest="sub", required=True)
    x = sn.add_parser("create"); x.add_argument("--project", required=True)
    x.add_argument("--session", action="append", required=True); x.set_defaults(fn=cmd_snapshot_create)
    bu = sub.add_parser("budget").add_subparsers(dest="sub", required=True)
    x = bu.add_parser("envelope"); x.add_argument("--project", required=True); x.add_argument("--name", required=True)
    x.add_argument("--unit", choices=["CAD_MICRO", "MOCK_MICRO"], required=True)
    x.add_argument("--limit-micro", type=int, required=True); x.add_argument("--parent")
    x.set_defaults(fn=cmd_budget_envelope)
    x = bu.add_parser("show"); x.add_argument("--project", required=True); x.set_defaults(fn=cmd_budget_show)
    x = bu.add_parser("reconcile"); x.add_argument("reservation"); x.add_argument("--actual-micro", type=int, required=True)
    x.add_argument("--note", required=True); x.set_defaults(fn=cmd_budget_reconcile)
    x = bu.add_parser("abandon-request"); x.add_argument("reservation"); x.add_argument("--evidence", required=True)
    x.add_argument("--sender-stopped", help="attestation that the original sender can no longer send"
                   " (required when its send permit is still open)")
    x.add_argument("--actor", required=True); x.set_defaults(fn=cmd_budget_abandon)
    x = bu.add_parser("record-response", help="record a late response that reached the operator, not the gateway")
    x.add_argument("reservation"); x.add_argument("--response-file", required=True)
    x.add_argument("--input-tokens", type=int, required=True); x.add_argument("--output-tokens", type=int, required=True)
    x.add_argument("--request-id", required=True, help="the provider's request id for this response")
    x.add_argument("--evidence", required=True, help="where the response and usage came from")
    x.add_argument("--actor", required=True); x.set_defaults(fn=cmd_budget_record_response)
    x = bu.add_parser("resolve-discrepancy"); x.add_argument("discrepancy"); x.add_argument("--note", required=True)
    x.add_argument("--actor", required=True); x.set_defaults(fn=cmd_budget_resolve)
    ru = sub.add_parser("run").add_subparsers(dest="sub", required=True)
    x = ru.add_parser("create"); x.add_argument("--project", required=True); x.add_argument("--snapshot", required=True)
    x.add_argument("--envelope", required=True); x.add_argument("--config", required=True)
    x.add_argument("--key", required=True); x.set_defaults(fn=cmd_run_create)
    x = ru.add_parser("work"); x.add_argument("run_id"); x.add_argument("--worker-id")
    x.add_argument("--lease-ttl", type=float, default=30.0); x.set_defaults(fn=cmd_run_work)
    x = ru.add_parser("unblock"); x.add_argument("run_id"); x.set_defaults(fn=cmd_run_unblock)
    x = sub.add_parser("report"); x.add_argument("run_id"); x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_report)
    x = sub.add_parser("trace"); x.add_argument("skill_version_id"); x.set_defaults(fn=cmd_trace)
    x = sub.add_parser("retrieve"); x.add_argument("--project", required=True); x.add_argument("--query", required=True)
    x.add_argument("--domain", required=True); x.add_argument("--tags", nargs="*")
    x.add_argument("--json", action="store_true"); x.set_defaults(fn=cmd_retrieve)
    ss = sub.add_parser("skill-set").add_subparsers(dest="sub", required=True)
    x = ss.add_parser("export"); x.add_argument("--project", required=True); x.add_argument("--set")
    x.add_argument("--out"); x.set_defaults(fn=cmd_skillset_export)
    x = ss.add_parser("verify"); x.add_argument("file"); x.add_argument("--digest", required=True)
    x.set_defaults(fn=cmd_skillset_verify)
    co = sub.add_parser("contracts").add_subparsers(dest="sub", required=True)
    x = co.add_parser("export"); x.add_argument("dir", nargs="?", default="contracts/v1")
    x.set_defaults(fn=cmd_contracts_export)
    x = sub.add_parser("demo"); x.add_argument("--slug"); x.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

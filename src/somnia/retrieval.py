"""Scope-filtered retrieval of procedural skills from a skill set.

Order of operations (MEMORY.md): project/skill-set filter -> applicability/exclusion filter ->
rank (exact vector distance, deterministic tie-break by version id) -> pack into the skill-count
and token ceilings. Every call is logged with eligible/selected/excluded version ids.
With the mock embedding spec, ranking is deterministic but NOT semantic.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .contracts import ExcludedSkill, RetrievalRequest, RetrievalResult, SelectedSkill
from .errors import IntegrityFailure
from .hashing import hash_obj
from .mock import MOCK_EMBEDDING_SPEC, mock_embedding, vector_literal
from .projects import active_skill_set_id, project_id as lookup_project

RETRIEVAL_CONFIG = {"version": 1, "ranking": "exact_cosine_distance", "tie_break": "skill_version_id",
                    "embedding_spec": MOCK_EMBEDDING_SPEC["id"]}


def _applicability(app: dict, exc: dict, domain: str, tags: set[str]) -> tuple[str, str] | None:
    if app.get("domain") not in (domain, "*"):
        return "out_of_domain", f"skill domain {app.get('domain')!r} != task domain {domain!r}"
    missing = set(app.get("requires_tags", [])) - tags
    if missing:
        return "missing_required_tags", f"task lacks required tags {sorted(missing)}"
    hit = set(exc.get("tags", [])) & tags
    if hit:
        return "excluded_by_scope", f"task has excluded tags {sorted(hit)}"
    return None


def retrieve_context(conn: psycopg.Connection, req: RetrievalRequest) -> RetrievalResult:
    pid = lookup_project(conn, req.project_slug)
    with conn.transaction():
        set_id = req.skill_set_id or active_skill_set_id(conn, pid)
        sset = conn.execute("SELECT id, version, contains_mock_validation FROM skill_sets"
                            " WHERE id = %s AND project_id = %s", (set_id, pid)).fetchone()
        if sset is None:
            raise LookupError("skill set does not belong to this project")
        sess = conn.execute("SELECT project_id FROM sessions WHERE id = %s", (req.session_id,)).fetchone()
        if sess is None or sess["project_id"] != pid:
            raise LookupError("session does not belong to this project")
        qvec = vector_literal(mock_embedding(req.task_query, MOCK_EMBEDDING_SPEC["dims"]))
        rows = conn.execute(
            "SELECT s.slug, v.id, v.version, v.title, v.procedure, v.applicability, v.exclusions, v.token_length,"
            " m.gate_decision_id, g.is_mock AS gate_is_mock, (e.embedding <=> %s::vector) AS distance"
            " FROM skill_set_members m JOIN skill_versions v ON v.id = m.skill_version_id"
            " JOIN skills s ON s.id = v.skill_id JOIN gate_decisions g ON g.id = m.gate_decision_id"
            " JOIN skill_embeddings e ON e.skill_version_id = v.id AND e.spec_id = %s"
            " WHERE m.skill_set_id = %s ORDER BY distance, v.id",
            (qvec, MOCK_EMBEDDING_SPEC["id"], set_id),
        ).fetchall()
        n_members = conn.execute("SELECT count(*) AS n FROM skill_set_members WHERE skill_set_id = %s",
                                 (set_id,)).fetchone()["n"]
        if n_members != len(rows):
            raise IntegrityFailure(f"{n_members - len(rows)} member(s) of set {set_id} lack a "
                                   f"{MOCK_EMBEDDING_SPEC['id']} embedding; refusing a silently partial retrieval")
        tags = set(req.scope.tags)
        selected: list[SelectedSkill] = []
        excluded: list[ExcludedSkill] = []
        eligible: list[str] = []
        tokens = 0
        for r in rows:
            verdict = _applicability(r["applicability"], r["exclusions"], req.scope.domain, tags)
            if verdict is not None:
                excluded.append(ExcludedSkill(skill_slug=r["slug"], skill_version_id=r["id"], reason=verdict[0],
                                              detail=verdict[1]))
                continue
            eligible.append(str(r["id"]))
            if len(selected) >= req.max_skills:
                excluded.append(ExcludedSkill(skill_slug=r["slug"], skill_version_id=r["id"], reason="max_skills",
                                              detail=f"limit {req.max_skills} reached"))
                continue
            if tokens + r["token_length"] > req.token_ceiling:
                excluded.append(ExcludedSkill(skill_slug=r["slug"], skill_version_id=r["id"], reason="token_ceiling",
                                              detail=f"{tokens}+{r['token_length']} > {req.token_ceiling}"))
                continue
            tokens += r["token_length"]
            ev = conn.execute("SELECT event_id, hypothesis_id, experiment_run_id, provider_call_id"
                              " FROM skill_version_evidence WHERE skill_version_id = %s ORDER BY id",
                              (r["id"],)).fetchall()
            refs = [f"gate_decision:{r['gate_decision_id']}"] + [
                f"{k.removesuffix('_id')}:{v}" for e in ev for k, v in e.items() if v is not None]
            selected.append(SelectedSkill(skill_slug=r["slug"], skill_version_id=r["id"], version=r["version"],
                                          title=r["title"], procedure=r["procedure"], token_length=r["token_length"],
                                          distance=float(r["distance"]), gate_decision_id=r["gate_decision_id"],
                                          gate_is_mock=r["gate_is_mock"], source_refs=refs))
        log = conn.execute(
            "INSERT INTO retrieval_logs (project_id, session_id, skill_set_id, request, config_hash, eligible,"
            " selected, excluded, token_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pid, req.session_id, set_id, Jsonb(req.model_dump(mode="json")), hash_obj(RETRIEVAL_CONFIG),
             Jsonb(eligible), Jsonb([str(s.skill_version_id) for s in selected]),
             Jsonb([e.model_dump(mode="json") for e in excluded]), tokens),
        ).fetchone()
    return RetrievalResult(retrieval_log_id=log["id"], skill_set_id=set_id, skill_set_version=sset["version"],
                           skill_set_contains_mock_validation=sset["contains_mock_validation"],
                           embedding_spec=MOCK_EMBEDDING_SPEC["id"], selected=selected, excluded=excluded,
                           token_count=tokens)


def render_context(result: RetrievalResult) -> str:
    """Text block a target agent would receive. Mock-validated skills are labelled as such."""
    if not result.selected:
        return "(no applicable skills)"
    lines = [f"# Skills from skill set v{result.skill_set_version}"
             + (" [contains MOCK-validated skills]" if result.skill_set_contains_mock_validation else "")]
    for s in result.selected:
        lines.append(f"## {s.title} ({s.skill_slug} v{s.version}{', mock-gated' if s.gate_is_mock else ''})")
        lines.append(s.procedure)
    return "\n".join(lines)

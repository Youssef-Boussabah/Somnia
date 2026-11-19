"""Immutable skill versions, gate decisions and skill-set exports."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .contracts import CandidateSkill, GateVerdict, SkillBody, SkillSetExport, SkillSetMember
from .errors import IntegrityFailure, translating
from .export_verify import export_digest
from .hashing import canonical_json, hash_obj
from .mock import MOCK_EMBEDDING_SPEC, approx_tokens, mock_embedding, vector_literal


def skill_content_hash(body: SkillBody, parent_version_id: UUID | None) -> str:
    return hash_obj({"body": body.model_dump(mode="json"), "parent_version_id": parent_version_id})


def skill_set_content_hash(members: list[dict[str, Any]]) -> str:
    """Hash over (skill_id, skill_version_id, gate_decision_id), order-independent."""
    norm = sorted(({"skill_id": str(m["skill_id"]), "skill_version_id": str(m["skill_version_id"]),
                    "gate_decision_id": str(m["gate_decision_id"])} for m in members),
                  key=lambda m: m["skill_id"])
    return hash_obj({"members": norm})


def render_skill_text(body: SkillBody) -> str:
    return f"{body.title}\n{body.procedure}\nScope: {canonical_json(body.applicability)}\n" \
           f"Exclusions: {canonical_json(body.exclusions)}\nLimits: {body.known_limits}"


def ensure_embedding_spec(conn: psycopg.Connection, spec: dict[str, Any] = MOCK_EMBEDDING_SPEC) -> None:
    conn.execute(
        "INSERT INTO embedding_specs (id, provider, model, revision, dims, normalized, is_mock)"
        " VALUES (%(id)s, %(provider)s, %(model)s, %(revision)s, %(dims)s, %(normalized)s, %(is_mock)s)"
        " ON CONFLICT (id) DO NOTHING", spec,
    )


def create_skill_version(conn: psycopg.Connection, *, project_id: UUID, body: SkillBody,
                         parent_version_id: UUID | None, origin: str, run_id: UUID | None) -> dict[str, Any]:
    """Insert a new immutable version (call inside the caller's transaction). Versions of a skill
    are numbered under a row lock on the skill so concurrent proposals cannot collide."""
    conn.execute("INSERT INTO skills (project_id, slug) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (project_id, body.slug))
    skill = conn.execute("SELECT id FROM skills WHERE project_id = %s AND slug = %s FOR UPDATE",
                         (project_id, body.slug)).fetchone()
    if parent_version_id is not None:
        parent = conn.execute("SELECT skill_id FROM skill_versions WHERE id = %s", (parent_version_id,)).fetchone()
        if parent is None or parent["skill_id"] != skill["id"]:
            raise IntegrityFailure("parent version belongs to a different skill")
    nxt = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM skill_versions WHERE skill_id = %s",
                       (skill["id"],)).fetchone()["v"]
    content_hash = skill_content_hash(body, parent_version_id)
    text = render_skill_text(body)
    row = conn.execute(
        "INSERT INTO skill_versions (skill_id, version, parent_version_id, title, procedure, applicability,"
        " exclusions, prerequisites, known_limits, content_hash, token_length, origin, created_by_run_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (skill["id"], nxt, parent_version_id, body.title, body.procedure, Jsonb(body.applicability),
         Jsonb(body.exclusions), Jsonb(body.prerequisites), body.known_limits, content_hash, approx_tokens(text),
         origin, run_id),
    ).fetchone()
    ensure_embedding_spec(conn)
    conn.execute(
        "INSERT INTO skill_embeddings (skill_version_id, spec_id, dims, embedding, source_hash)"
        " VALUES (%s, %s, %s, %s::vector, %s)",
        (row["id"], MOCK_EMBEDDING_SPEC["id"], MOCK_EMBEDDING_SPEC["dims"],
         vector_literal(mock_embedding(text, MOCK_EMBEDDING_SPEC["dims"])), hash_obj(text)),
    )
    return {"skill_version_id": row["id"], "skill_id": skill["id"], "version": nxt, "content_hash": content_hash}


def _body_from_row(r: dict[str, Any]) -> SkillBody:
    return SkillBody(slug=r["slug"], title=r["title"], procedure=r["procedure"], applicability=r["applicability"],
                     exclusions=r["exclusions"], prerequisites=r["prerequisites"], known_limits=r["known_limits"])


_VERSION_SQL = (
    "SELECT v.id, v.skill_id, v.version, v.parent_version_id, v.title, v.procedure, v.applicability, v.exclusions,"
    " v.prerequisites, v.known_limits, v.content_hash, v.token_length, v.origin, s.slug, s.project_id"
    " FROM skill_versions v JOIN skills s ON s.id = v.skill_id"
)


def load_candidate(conn: psycopg.Connection, version_id: UUID, *, verify: bool = True) -> CandidateSkill:
    r = conn.execute(_VERSION_SQL + " WHERE v.id = %s", (version_id,)).fetchone()
    if r is None:
        raise LookupError(f"unknown skill version {version_id}")
    body = _body_from_row(r)
    if verify and skill_content_hash(body, r["parent_version_id"]) != r["content_hash"]:
        raise IntegrityFailure(f"skill version {version_id} content does not match its stored hash")
    return CandidateSkill(skill_version_id=r["id"], skill_id=r["skill_id"], version=r["version"],
                          parent_version_id=r["parent_version_id"], content_hash=r["content_hash"],
                          token_length=r["token_length"], origin=r["origin"], body=body)


def record_gate_decision(conn: psycopg.Connection, *, skill_version_id: UUID, baseline_skill_set_id: UUID,
                         verdict: GateVerdict, run_id: UUID | None) -> UUID:
    """Persist an immutable gate decision. The DB rejects it unless candidate_hash matches."""
    with translating():
        row = conn.execute(
            "INSERT INTO gate_decisions (skill_version_id, candidate_hash, baseline_skill_set_id, validator,"
            " validator_version, suite_id, suite_hash, independent, is_mock, decision, reason, paired_scores,"
            " evidence_refs, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (skill_version_id, verdict.candidate_hash, baseline_skill_set_id, verdict.validator,
             verdict.validator_version, verdict.suite_id, verdict.suite_hash, verdict.independent, verdict.is_mock,
             verdict.decision, verdict.reason, Jsonb([s.model_dump(mode="json") for s in verdict.paired_scores]),
             Jsonb(verdict.evidence_refs), run_id),
        ).fetchone()
    return row["id"]


def set_members(conn: psycopg.Connection, skill_set_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT m.skill_id, m.skill_version_id, m.gate_decision_id, g.is_mock AS gate_is_mock,"
        " g.independent AS gate_independent FROM skill_set_members m"
        " JOIN gate_decisions g ON g.id = m.gate_decision_id WHERE m.skill_set_id = %s", (skill_set_id,),
    ).fetchall()


def export_skill_set(conn: psycopg.Connection, skill_set_id: UUID) -> SkillSetExport:
    """Frozen, verifiable view of a skill set for evaluators and reports."""
    s = conn.execute("SELECT * FROM skill_sets WHERE id = %s", (skill_set_id,)).fetchone()
    if s is None:
        raise LookupError(f"unknown skill set {skill_set_id}")
    raw = set_members(conn, skill_set_id)
    if skill_set_content_hash(raw) != s["content_hash"]:
        raise IntegrityFailure(f"skill set {skill_set_id} membership does not match its stored hash")
    members = []
    for m in raw:
        c = load_candidate(conn, m["skill_version_id"])
        if c.skill_id != m["skill_id"]:
            raise IntegrityFailure(f"skill set {skill_set_id} member {m['skill_version_id']} has a mismatched skill")
        members.append(SkillSetMember(skill_slug=c.body.slug, skill_id=c.skill_id, skill_version_id=c.skill_version_id,
                                      version=c.version, parent_version_id=c.parent_version_id,
                                      content_hash=c.content_hash, gate_decision_id=m["gate_decision_id"],
                                      gate_is_mock=m["gate_is_mock"], gate_independent=m["gate_independent"],
                                      token_length=c.token_length, body=c.body))
    members.sort(key=lambda x: x.skill_slug)
    try:
        return SkillSetExport(skill_set_id=s["id"], project_id=s["project_id"], version=s["version"],
                              predecessor_id=s["predecessor_id"], content_hash=s["content_hash"],
                              contains_mock_validation=s["contains_mock_validation"], members=members)
    except ValueError as exc:  # pydantic ValidationError: stored flags disagree with the members
        raise IntegrityFailure(f"skill set {skill_set_id} validation flags are inconsistent: {exc}") from exc


def skill_set_export_digest(export: SkillSetExport) -> str:
    """The digest an evaluator must obtain through a trusted channel to verify ``export``
    (SHA-256 of the canonical JSON of the whole export; see export_verify)."""
    return export_digest(export.model_dump(mode="json"))

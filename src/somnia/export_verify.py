"""Standalone verifier for frozen skill-set exports (SkillSetExport v2).

Python standard library only: no database, no Somnia or pydantic imports. An evaluator can copy
this file and verify a set using only the exported JSON plus a digest it obtained separately
from a trusted channel (for example, recorded in the evaluation protocol when the set was frozen).

Digest rule ("export digest", canonicalization ``somnia-cjson-v1``):
    digest = SHA-256( UTF-8( canonical_json(document) ) ), lowercase hex
    canonical_json = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
The digest covers the WHOLE parsed document: every top-level field and every member field,
including member bodies, ids, versions, inner hashes and validation flags. No field is excluded,
and the document never contains its own digest. Whitespace and key order of the file do not
matter because the document is parsed and re-serialized canonically.

Edits are caught in two independent ways:
  1. the recomputed export digest must equal the trusted digest (catches any edit, including one
     whose author also recomputed every inner hash);
  2. internal consistency: member content hashes, the membership hash and the mock-validation
     flags are recomputed from the content itself (catches naive edits even without a digest).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

CANONICALIZATION = "somnia-cjson-v1"
SCHEMA_VERSION = 2


class ExportVerificationError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("skill-set export failed verification: " + "; ".join(problems))
        self.problems = problems


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def export_digest(document: dict[str, Any]) -> str:
    """Digest of a parsed export document (see module docstring)."""
    return _sha256(document)


def member_content_hash(member: dict[str, Any]) -> str:
    """Skill-version content hash: binds the body and the parent version id."""
    return _sha256({"body": member["body"], "parent_version_id": member["parent_version_id"]})


def membership_hash(members: list[dict[str, Any]]) -> str:
    """Skill-set content hash: binds which version (and which gate decision) each skill has."""
    norm = sorted(({"skill_id": m["skill_id"], "skill_version_id": m["skill_version_id"],
                    "gate_decision_id": m["gate_decision_id"]} for m in members), key=lambda m: m["skill_id"])
    return _sha256({"members": norm})


def _parse(document: bytes | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(document, dict):
        return document
    if isinstance(document, bytes):
        document = document.decode("utf-8")
    return json.loads(document)


def verify_skill_set_export(document: bytes | str | dict[str, Any], trusted_digest: str) -> dict[str, Any]:
    """Return the parsed document if it verifies; raise ExportVerificationError otherwise."""
    try:
        doc = _parse(document)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportVerificationError([f"not valid UTF-8 JSON: {exc}"]) from exc
    if not isinstance(doc, dict):
        raise ExportVerificationError(["document is not a JSON object"])
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ExportVerificationError([
            f"schema_version {doc.get('schema_version')!r} is not {SCHEMA_VERSION}; v1 exports carry no "
            "parent/skill ids or canonicalization and cannot be verified independently (re-export as v2)"])
    problems: list[str] = []
    if doc.get("canonicalization") != CANONICALIZATION:
        problems.append(f"unknown canonicalization {doc.get('canonicalization')!r}")
    want = trusted_digest.strip().lower().removeprefix("sha256:")
    try:
        got = export_digest(doc)
    except ValueError as exc:  # NaN/Infinity cannot be canonicalized
        raise ExportVerificationError([f"cannot canonicalize document: {exc}"]) from exc
    if not hmac.compare_digest(got, want):
        problems.append("export digest does not match the trusted digest")
    try:
        members = doc["members"]
        seen_skills, seen_versions = set(), set()
        for m in members:
            label = f"member {m.get('skill_slug')!r}"
            if member_content_hash(m) != m["content_hash"]:
                problems.append(f"{label}: content hash does not match its body and parent")
            if m["body"].get("slug") != m["skill_slug"]:
                problems.append(f"{label}: body slug differs from skill_slug")
            if m["gate_is_mock"] is True and m["gate_independent"] is True:
                problems.append(f"{label}: a mock gate cannot be independent")
            if not (isinstance(m["gate_is_mock"], bool) and isinstance(m["gate_independent"], bool)):
                problems.append(f"{label}: validation flags must be booleans")
            if m["skill_id"] in seen_skills or m["skill_version_id"] in seen_versions:
                problems.append(f"{label}: duplicate skill or skill version")
            seen_skills.add(m["skill_id"])
            seen_versions.add(m["skill_version_id"])
        if membership_hash(members) != doc["content_hash"]:
            problems.append("set content hash does not match the membership")
        if doc["contains_mock_validation"] is not any(m["gate_is_mock"] is True for m in members):
            problems.append("contains_mock_validation does not match the members' gate_is_mock flags")
    except (KeyError, TypeError, AttributeError) as exc:
        problems.append(f"malformed export: {type(exc).__name__}: {exc}")
    if problems:
        raise ExportVerificationError(problems)
    return doc


def scientific_use_blockers(doc: dict[str, Any]) -> list[str]:
    """Reasons a VERIFIED export must not be used for a scientific comparison (empty = no blocker
    from the export itself; the evaluation protocol may impose more)."""
    out = []
    if doc["contains_mock_validation"]:
        out.append("set contains mock-validated skills")
    out += [f"member {m['skill_slug']!r} was not independently validated"
            for m in doc["members"] if not m["gate_independent"]]
    return out

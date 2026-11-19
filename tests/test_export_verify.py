"""Frozen skill-set exports verify from exported JSON plus a separately trusted digest, with no database.

The tamper cases model an author who edits the export AND recomputes every inner hash field. Only
the digest obtained through a separate trusted channel can catch that; the tests check it does."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, make_run
from somnia.engine import SleepWorker
from somnia.export_verify import (ExportVerificationError, canonical_json, export_digest, member_content_hash,
                                  membership_hash, scientific_use_blockers, verify_skill_set_export)
from somnia.hashing import canonical_json as somnia_canonical_json
from somnia.projects import active_skill_set_id
from somnia.skills import export_skill_set, skill_set_export_digest

DIGEST_ONLY = ["export digest does not match the trusted digest"]


@pytest.fixture
def exported(conn, artifacts, project):
    run = make_run(conn, project["id"])
    assert SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"]).status == "completed"
    exp = export_skill_set(conn, active_skill_set_id(conn, project["id"]))
    assert exp.schema_version == 2 and len(exp.members) == 1  # a nonempty set
    return exp.model_dump_json(indent=2).encode("utf-8"), skill_set_export_digest(exp)


def _reseal(doc):
    """What a careful tamperer does: recompute every hash field inside the document."""
    for m in doc["members"]:
        m["content_hash"] = member_content_hash(m)
    doc["content_hash"] = membership_hash(doc["members"])
    return doc


def _problems(doc, digest):
    with pytest.raises(ExportVerificationError) as exc:
        verify_skill_set_export(json.dumps(doc).encode("utf-8"), digest)
    return exc.value.problems


def test_valid_export_verifies_from_json_and_trusted_digest(exported):
    data, digest = exported
    doc = verify_skill_set_export(data, digest)
    # Formatting and key order of the file do not matter; the "sha256:" prefix is accepted.
    reformatted = json.dumps(json.loads(data), indent=None, sort_keys=False)
    assert verify_skill_set_export(reformatted, f"sha256:{digest}") == doc
    # The verifier's stdlib canonical form is byte-identical to the learner's hashing.
    assert canonical_json(doc) == somnia_canonical_json(doc) and export_digest(doc) == digest
    # Verified, but still not usable for a scientific comparison: it is mock-validated.
    blockers = scientific_use_blockers(doc)
    assert "set contains mock-validated skills" in blockers and len(blockers) == 2


def test_edited_procedure_fails_even_with_recomputed_inner_hashes(exported):
    data, digest = exported
    doc = json.loads(data)
    doc["members"][0]["body"]["procedure"] += " Skip the flush when the disk is slow."
    naive = _problems(doc, digest)
    assert "export digest does not match the trusted digest" in naive
    assert any("content hash does not match" in p for p in naive)
    assert _problems(_reseal(doc), digest) == DIGEST_ONLY
    # The digest is the trust root: a digest taken from the edited document itself would "verify".
    # That is why it must come from a separate trusted channel, never from the export.
    verify_skill_set_export(json.dumps(doc), export_digest(doc))


def test_flipped_mock_validation_flag_fails_even_with_recomputed_inner_hashes(exported):
    data, digest = exported
    doc = json.loads(data)
    doc["contains_mock_validation"] = False
    flipped = _problems(_reseal(doc), digest)
    assert "export digest does not match the trusted digest" in flipped
    assert any("contains_mock_validation" in p for p in flipped)
    # Flipping every flag consistently (set and member, mock -> independent) leaves only the digest.
    m = doc["members"][0]
    m["gate_is_mock"], m["gate_independent"] = False, True
    assert _problems(_reseal(doc), digest) == DIGEST_ONLY


def test_v1_export_is_refused(exported):
    data, digest = exported
    doc = json.loads(data)
    doc["schema_version"] = 1
    problems = _problems(doc, digest)
    assert len(problems) == 1 and "v1 exports" in problems[0]


def test_cli_export_then_offline_verify(conn, artifacts, project, db_url, tmp_path):
    run = make_run(conn, project["id"])
    assert SleepWorker(conn, artifacts, worker_id="w").work(run["run_id"]).status == "completed"
    out = tmp_path / "set.json"

    def cli(*args, url=db_url):
        env = {**os.environ, "SOMNIA_DATABASE_URL": url}
        return subprocess.run([sys.executable, "-m", "somnia.cli", *args], cwd=ROOT, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=120)

    p = cli("skill-set", "export", "--project", project["slug"], "--out", str(out))
    assert p.returncode == 0, p.stderr
    digest = p.stdout.strip().removeprefix("export_digest ")
    # Verification uses no database: point the CLI at an unreachable one.
    offline = "postgresql://nobody:x@127.0.0.1:9/none"
    ok = cli("skill-set", "verify", str(out), "--digest", digest, url=offline)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert json.loads(ok.stdout)["ok"] is True
    out.write_text(out.read_text(encoding="utf-8").replace("Make output durable", "Make output fast"),
                   encoding="utf-8")
    bad = cli("skill-set", "verify", str(out), "--digest", digest, url=offline)
    assert bad.returncode == 1 and json.loads(bad.stdout)["ok"] is False

# Somnia public interfaces

Status: implemented for the foundation (mock-only); revised 2026-11-19 after review of `4841892`.
The JSON Schemas in `v1/` are generated from `src/somnia/contracts.py` (`uv run somnia contracts export`), and a test fails if they drift.
`v1/` is the directory of the contract bundle. Each schema has its own `schema_version`:

| Contract | Version | Change on 2026-11-19 |
|---|---|---|
| `SkillSetExport` | **2** | Can now be verified independently; adds `canonicalization` and members' `skill_id` and `parent_version_id` (D035). |
| `GateRequest` | **2** | Embeds a `SkillSetExport` v2 as `baseline_skill_set` (D035). |
| `GateVerdict` | 1 | No field changed. `is_mock=true` with `independent=true` is now rejected by the model and by the schema (`not` clause), as well as by the database (D037). |
| all others | 1 | Unchanged. |

These contracts are proposed for joint review with the independent evaluator (Codex).
The reference-agent interface is **not defined yet**; it still has to be agreed with the evaluator.

All examples below are **development outputs from synthetic fixtures and mock components**.
They are not measurements, and their numbers (including paired scores and distances) mean nothing scientifically.

## What the evaluator needs

| Need | Contract | How to obtain / provide |
|---|---|---|
| Read a frozen skill set | `SkillSetExport` v2 | `somnia skill-set export --project P [--set ID] [--out FILE]` prints the JSON and, separately, an `export_digest`. Verify the JSON offline (below). Refuse any set with scientific-use blockers for scientific comparisons. |
| Validate a candidate | `GateRequest` v2 → `GateVerdict` | Implement `interfaces.GateValidator` outside the learner's process and credentials. Return `independent=true, is_mock=false` only for genuinely independent suites. Somnia stores the verdict with `skills.record_gate_decision()`, bound to the baseline set it was validated against. |
| Retrieve context like the target agent | `RetrievalRequest` → `RetrievalResult` | `retrieval.retrieve_context()` / `somnia retrieve`. Development and validation use only; see "Sealed-task retrieval". |
| Execute a scenario | `ScenarioSpec` → `ExecutionResult` | `interfaces.ScenarioExecutor`. Only `MockExecutor` exists, and it executes no code. |

What the evaluator never sends to the learner: hidden oracle code, sealed-task content, or per-item final results.
A `GateVerdict` carries a decision, a bounded reason, a suite id and hash, and paired scores.
Whether paired scores may be shown to the learner for non-development suites is an **open question** for the evaluation protocol (EXPERIMENTS.md).

## Verifying a frozen export (SkillSetExport v2)

The evaluator needs two things:
- the exported JSON;
- the **export digest**, obtained through a channel the learner cannot rewrite, for example recorded in the evaluation protocol when the set is frozen.

The digest is never part of the JSON. Verification needs no database:

```
uv run somnia skill-set verify set.json --digest sha256:<hex>
# or, from Python with only the standard library: src/somnia/export_verify.py (designed to be copied)
from export_verify import verify_skill_set_export, scientific_use_blockers
doc = verify_skill_set_export(open("set.json", "rb").read(), trusted_digest)   # raises on any problem
assert scientific_use_blockers(doc) == []   # else: mock-validated or not independently validated
```

**Canonicalization `somnia-cjson-v1`:**
1. Parse the file as JSON.
2. Re-serialize it as `json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`: keys sorted by code point, no insignificant whitespace, non-ASCII emitted as raw UTF-8, Python's string escaping, and NaN/Infinity rejected.
3. Encode the result as UTF-8.

The file's own formatting and key order therefore do not matter. Non-Python verifiers must reproduce Python's escaping and integer formatting; the export contains no floats except inside skill-body `applicability`/`exclusions` values, if an author puts them there.

**What the digest covers:** SHA-256, lowercase hex, of the canonical bytes of the **entire** document. No field is excluded. That includes:
- `schema_version`, `canonicalization`, the set id, project id, version and predecessor;
- the set `content_hash` and `contains_mock_validation`;
- for every member: `skill_slug`, `skill_id`, `skill_version_id`, `version`, `parent_version_id`, `content_hash`, `gate_decision_id`, `gate_is_mock`, `gate_independent`, `token_length` and the full `body`.

Changing anything changes the digest.

The verifier also recomputes, from the content alone:
- each member's `content_hash` = sha256(canonical `{"body": body, "parent_version_id": parent}`);
- the set's `content_hash` = sha256(canonical `{"members": [{skill_id, skill_version_id, gate_decision_id}, ...]}`, sorted by `skill_id`);
- `contains_mock_validation == any(member.gate_is_mock)`, and that no member is both mock and independent.

These internal checks catch naive edits. Only the trusted digest catches an edit whose author also recomputed every inner hash. A digest copied from an untrusted source proves nothing.

**Existing v1 exports:** the verifier refuses `schema_version: 1`. A v1 export has no `parent_version_id`, no `skill_id` and no canonicalization tag, so its hashes cannot be recomputed from the document. The evaluator must not use a v1 export for any comparison. To recover:
1. Re-export the same `skill_set_id` as v2 from the learner database; the underlying records are immutable.
2. Check that the v1 and v2 documents agree on `skill_set_id`, the set `content_hash` and each member's `skill_version_id` and `content_hash`.
3. Record the v2 digest through the trusted channel.

## Gate verdict consistency

`is_mock=true` together with `independent=true` is rejected in three places:
- the pydantic model;
- the exported JSON Schema (`"not": {"required": ["is_mock","independent"], "properties": {"is_mock": {"const": true}, "independent": {"const": true}}}`);
- the PostgreSQL CHECK `NOT (is_mock AND independent)`.

A verdict only promotes a candidate while the project's active set is **exactly** the `baseline_skill_set` it was validated against. After any other activation, the verdict is stale (`StaleValidation`) and the candidate needs fresh validation (D032).

## Example: `GateVerdict` from the mock gate (development)

```json
{
  "schema_version": 1,
  "candidate_hash": "97912dd12c71ac15…",
  "decision": "pass",
  "reason": "MOCK GATE (not independent validation): scripted pass for development",
  "validator": "somnia.mock_gate",
  "validator_version": "0.1.0",
  "suite_id": "mock-suite-v1",
  "suite_hash": "…",
  "independent": false,
  "is_mock": true,
  "paired_scores": [{"task_id": "dev-gate-fixture-a", "root_cluster_id": "dev-root-1", "repeat": 1,
                     "without_skill": 0.0, "with_skill": 1.0, "synthetic": true}],
  "evidence_refs": ["mock_script:gate_verdicts/durable-output-before-progress"]
}
```

## Example: fresh-session retrieval (development, `somnia retrieve … --json`)

Input: `--project smoke-1 --query "resumable job writing results and a checkpoint" --domain python-reliability --tags resumable local-persistence`

```json
{
  "schema_version": 1,
  "skill_set_version": 1,
  "skill_set_contains_mock_validation": true,
  "embedding_spec": "mock-hash-v1",
  "selected": [{
    "skill_slug": "durable-output-before-progress",
    "version": 1,
    "title": "Make output durable before advancing progress",
    "token_length": 133,
    "distance": 0.6932808307317473,
    "gate_is_mock": true,
    "source_refs": ["gate_decision:a6911963-…", "hypothesis:9fa5e0e3-…", "experiment_run:e1fd2b7e-…",
                    "experiment_run:09b862b7-…", "event:9500d339-…", "event:d42a3e0a-…", "provider_call:392a2bde-…"]
  }],
  "excluded": [],
  "token_count": 133
}
```

With the tag `irreversible-external-effect` added, the same skill is returned under `excluded` with reason `excluded_by_scope`. With domain `web`, the reason is `out_of_domain`.
`mock-hash-v1` is feature hashing: deterministic but not semantic, so `distance` carries no meaning.

## Sealed-task retrieval must run on the evaluator side

The development retrieval path (`retrieve_context`, `somnia retrieve`) writes every request, **including the full task query**, to the learner-side `retrieval_logs` table, and opens a session in the learner database.
Running it on a sealed final task would put sealed-task text into the learner's store.

Future sealed-task retrieval must therefore run inside the evaluator's environment, against a verified frozen `SkillSetExport` v2, with its own logs.
No learner-side process may see the queries. The development path is for development and validation partitions only (D039).

## Experience citations inside the sleep cycle (not an evaluator contract)

The consolidate prompt (`consolidate.v2`) cites events by snapshot-local references `s<session ordinal>/<idempotency key>`, because keys repeat across sessions. Hypothesis evidence stores the resolved event id (D034).

## Stability rules
- Changing a field is a contract change: bump that model's `schema_version`, re-export, and record the change in DECISIONS.md.
- Report JSON (`somnia report RUN --json`) has `report_schema_version: 2` (D045: `budget_alerts` and the outstanding-request / billing-discrepancy entries under `unresolved`) but no JSON Schema yet. The dashboard should treat it as provisional until a typed report contract is agreed.
- Inside the archived report artifact, the report stage's own `output_hash` is `null`, because a report cannot contain the hash of the bytes it is serialized into (D036).

# Somnia public interfaces — contract v1

Status: implemented for the foundation (mock-only) on 2026-11-18. JSON Schemas in `v1/` are generated
from `src/somnia/contracts.py` (`uv run somnia contracts export`). A test fails if they drift.
Proposed for joint review with the independent evaluator (Codex). The reference-agent interface is
**not defined yet**: it still has to be agreed with the evaluator.

All examples below are **development outputs from synthetic fixtures and mock components**. They
are not measurements, and their numbers (including paired scores and distances) mean nothing scientifically.

## What the evaluator needs

| Need | Contract | How to obtain / provide |
|---|---|---|
| Read a frozen skill set | `SkillSetExport` | `somnia skill-set export --project P [--set ID]` or `skills.export_skill_set()`. Re-verify `content_hash`; refuse sets with `contains_mock_validation=true` for any scientific comparison. |
| Validate a candidate | `GateRequest` → `GateVerdict` | Implement `interfaces.GateValidator` outside the learner's process and credentials. Return `independent=true, is_mock=false` only for genuinely independent suites. Somnia stores the verdict with `skills.record_gate_decision()`; the database rejects verdicts whose `candidate_hash` does not match the stored candidate, and any verdict that claims to be both mock and independent. |
| Retrieve context like the target agent | `RetrievalRequest` → `RetrievalResult` | `retrieval.retrieve_context()` / `somnia retrieve`. Pass an explicit `skill_set_id` to pin a frozen set. Every call is logged (`retrieval_logs`) with its eligible, selected and excluded version ids. |
| Execute a scenario | `ScenarioSpec` → `ExecutionResult` | `interfaces.ScenarioExecutor`. Only `MockExecutor` exists; it executes no code. |

What the evaluator never sends to the learner: hidden oracle code, sealed-task content, or per-item
final results. A `GateVerdict` carries a decision, a bounded reason, a suite id/hash and paired scores.
Whether those paired scores may be shown to the learner for non-development suites is an **open
question** for the evaluation protocol (EXPERIMENTS.md).

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

With the tag `irreversible-external-effect` added, the same skill is returned under `excluded` with
reason `excluded_by_scope`. With domain `web`, the reason is `out_of_domain`. `mock-hash-v1` is
feature hashing: deterministic but not semantic, so `distance` carries no meaning.

## Stability rules
- Changing a field is a contract change: bump the model's `schema_version`, re-export, and record the
  change in DECISIONS.md.
- Report JSON (`somnia report RUN --json`) has `report_schema_version: 1` but no JSON Schema yet. The
  dashboard should treat it as provisional until a typed report contract is agreed.

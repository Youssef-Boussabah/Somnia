# Somnia — research journal
Version 0.1

## 2026-11-18 — Documentation baseline
Type: planning and source verification.

Completed: consolidated user decisions; drafted the project, architecture, memory, offline-learning and evaluation specifications; defined budget policy, ownership and handoffs; prepared ChatGPT Project context/instructions.

Primary sources checked: official SkillOpt repository and Sleep documentation; Anthropic memory article; Letta sleep-time compute article; ACE paper abstract; PostgreSQL JSONB documentation; pgvector documentation; Inspect sandboxing documentation. See RESEARCH.md for links and claim boundaries.

Findings: substantial prior art overlaps with offline consolidation, replay and gated skills. The narrower contribution remains hypothetical. Competitor execution capabilities need a pinned audit. Promotion validation and sealed final evaluation must be different data boundaries.

Not performed: source-code reproduction; repository creation; infrastructure provisioning; migrations; task generation; paid model calls; experiments; dashboard implementation.

Results: none. Product test count: none. Scientific effect size: not measured. This documentation task incurred no experimental model API spending; account balances have not been audited.

## Entry template
Date/time and author:
Study/run ID:
Question and prior expectation:
Decision/protocol version:
Code, dataset, model and environment hashes:
What actually ran:
Observations and artifact links:
Failures, exclusions and negative results:
Measured cost and unresolved liability:
Interpretation and uncertainty:
Changes justified by evidence:
Next action:

Do not overwrite an earlier observation after discovering an error. Append a correction and link the superseded record.

## 2026-11-18 — Foundation implementation (Assignment A)
Type: engineering. **Not an experiment; no research result.**
Author: Claude Code (lead engineer session).
Question: can the smallest complete learning cycle run durably on PostgreSQL + pgvector, using mocks only, with correct resume, budget and activation semantics?
Code/environment: branch `foundation`, commits `e641d62` and `6704a22`; CPython 3.13.14, psycopg 3.3.9, pydantic 2.14.3, pytest 9.2.0 (`uv.lock`); PostgreSQL 17.12 + pgvector 0.8.7 image `pgvector/pgvector:0.8.7-pg17@sha256:b513c485…bd8fd`.

What actually ran:
- `uv run somnia demo --slug smoke-1` on the development database. This completed all 7 stages using the synthetic indexer-checkpoint fixture: 2 mock hypotheses; 2 scenarios (1 valid, 1 invalid by scripted check); 2 mock rehearsal attempts; 2 candidates; mock gate 1 pass / 1 fail; 1 activation (set v0 → v1, flagged `contains_mock_validation`). Fresh-session retrieval returned `durable-output-before-progress` v1. Mock ledger: 5,012 MOCK_MICRO confirmed (fake units). Zero outstanding reservations.
- `uv run pytest`: 38 passed (final run 20.95 s). Each pytest session creates and drops its own real PostgreSQL database. The full suite passed three consecutive times before the final test was added.
- Required checks, each covered by a test against real PostgreSQL:
  - interrupted run resumes without duplicate activation (`test_crash_after_activation_resumes_without_duplicate_activation`);
  - concurrent reservations cannot exceed the budget. 40 racing reservations against a 100,000 limit: 14 accepted, 26 rejected. Hierarchical variant: the parent limit holds across sibling envelopes;
  - uncertain billing retains its reservation (provider-unknown-outcome test, and a crash between reserve and send). The run stays blocked until an explicit reconcile;
  - an expired worker cannot overwrite a newer worker's progress, both when the successor has already committed and while the successor is still mid-stage;
  - a stale parent version cannot be promoted, through both the engine path and a direct call with an up-to-date expected set;
  - artifacts trace to inputs, and the trace detects a corrupted response artifact and a superuser event edit made with triggers bypassed;
  - a fresh session retrieves the expected active version: a separate CLI process retrieves v2 after a revision run, and pinning the earlier set retrieves v1.
- Mutation checks (single-guard removals, each run against its targeted test):
  - removing the budget guard, the stale-parent check, orphan-reservation marking, activation idempotency replay, or the DB ban on `uncertain → released`: the targeted test failed each time;
  - removing the commit-time fence check: **the targeted test still passed**. The stage-pointer check masked it. I added `test_expired_worker_cannot_commit_while_successor_is_mid_stage` and a row-count assertion on the pointer update. With both guards removed, 2 lease tests fail. With only one removed, the other guard still holds.

Failures, exclusions and negative results:
- The first full test run had 1 failure. Provenance tracing did not verify the consolidate call that produced the cited hypotheses. I fixed the trace, not the test.
- Not implemented: real providers, real embeddings, sandboxed execution, reference agent interface, HTTP API, learner/evaluator role separation, bounded stage retries, code-commit/dirty-tree capture in run manifests.

Measured cost and unresolved liability: CAD 0 experimental spend. No CAD envelopes. 0 outstanding reservations.
Interpretation: this demonstrates infrastructure semantics under mocks. It says nothing about whether Somnia learns, and the mock gate is not validation.
Changes justified by evidence: the two fixes above (trace coverage; fencing test plus row-count guard).
Next action: Assignment D independent review of `foundation` before pushing; agree the reference agent interface with the evaluator.

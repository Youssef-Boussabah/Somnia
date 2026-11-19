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

## 2026-11-19 — Foundation repairs after review of `4841892`
Type: engineering. **Not an experiment; no research result.**
Author: Claude Code (lead engineer session).
Baseline: `4841892`, clean working tree, no stash. Repairs are uncommitted in the working tree, for the user to review and commit (D041).
Question: do the six review findings reproduce, and do the repairs remove them without breaking resume, budget or activation semantics?

What actually ran:
- I wrote the new regression tests first and ran them against the unrepaired code. All 12 tests for findings 1, 2, 3, 5 and 6 failed. 8 failed on the defect itself:
  1. Rollback to v0, then activation: `UniqueViolation (project_id, version)=(…, 1)`.
  2. Two passing candidates on the same baseline: both were activated. Separately, a gate decision from project A activated into project B without error.
  3. Two sessions reusing keys `evt-001`…`evt-005`: both hypotheses' sources resolved to the **second** session's events.
  4. Archived report bytes: `status: running`, `current_stage: report`. This held for a normal run and after a crash plus resume in the report stage.
  5. `GateVerdict(is_mock=true, independent=true)` was accepted by the pydantic model and by a Draft 2020-12 validator using the committed schema.

  The other 4 were citation tests written in the new `consolidate.v2` format; they failed on the missing response, not on the defect. The 5 finding-4 export tests and the 2 migration tests need interfaces added by the fix, so they had no "before" run.
- After the repairs, `uv run pytest` gave **57 passed, 0 failed, 0 skipped** (29.1 s). The 19 tests added to the previous 38 are:
  - 3 activation tests;
  - 5 citation tests;
  - 2 archived-report tests;
  - 2 gate-verdict tests;
  - 5 export-verification tests;
  - 2 upgrade tests.
- One existing test changed by design: `test_stale_parent_version_cannot_be_promoted`.
  - Its engine path now reports `stale_validation`, and a direct activation with the old verdict raises `StaleValidation`.
  - The stale-parent guard is still exercised: a fresh verdict against the current set still raises `StaleParentVersion`.
- Schemas were generated into a scratch directory and diffed against the HEAD schemas. Only `gate_request`, `gate_verdict` and `skill_set_export` changed, and only as intended. The generated files were then installed into `contracts/v1/`, and the drift test confirms that the working-tree schemas match the models.
- Upgrade check: I made a TEMPLATE copy of the development database (at 0001: 1 project, 1 run, 2 skill sets, 2 activations).
  - 0002 applied, and its NOTICE reported 0 pre-0002 activations replacing a set other than their gate baseline.
  - Both sets exported as v2 and verified; both skill-version traces passed.
  - The copy was then dropped.
  - In tests, an upgrade from a 0001-only database kept export digests identical, and the migration refused a 0001 database containing a cross-project membership.
- `uv run somnia demo`:
  - completed 7 stages and migrated the development database to 0002;
  - its archived report bytes read `completed`/`done` with the report stage `succeeded`;
  - `skill-set export --out` followed by `skill-set verify` returned `ok: true` with two scientific-use blockers (mock-validated; not independent).

Failures, exclusions and negative results:
- The trusted channel for export digests is not defined. Verification is only as strong as that channel (D035, open blocker).
- Pending runs created before the fixture change fail closed and must be recreated (D034). The development database had none.
- No mutation run was repeated for the new guards beyond the before-fix failures. The Python checks of D032 and D033 are also backed by 0002 triggers, so removing one layer alone would still be caught, but by a different error.

Measured cost and unresolved liability: CAD 0 experimental spend. No CAD envelopes. 0 outstanding reservations.
Interpretation: infrastructure correctness under mocks only; nothing here measures learning.
Next action: Codex re-review of the repairs; the user commits and pushes.

## 2026-11-19 — Repair after review of `b87707c`: replay bound to operation kind
Type: engineering. **Not an experiment; no research result.** Author: Claude Code.
Base: `b87707c` on `foundation`, clean working tree, no stash. The repair is uncommitted (D041).
Finding: a rollback could replay an activation's idempotency key.

What actually ran:
- New regression test `test_rollback_cannot_replay_an_activation_key` (real PostgreSQL, public functions only):
  1. activate v1 with key K;
  2. advance to v2 through a revision sleep run;
  3. call `rollback_to(target=v1, key=K)`.
- **Before the fix:** `Failed: DID NOT RAISE IdempotencyConflict`. A scratch probe outside the repository repeated the same steps and recorded the return value:
  - `applied=False`;
  - `activation_id` = the v1 activation;
  - `new_set_version=1`;
  - pointer still v2;
  - 0 new activation rows.

  The caller received a false "already rolled back" receipt.
- **Second new test**, `test_rollback_retry_returns_original_receipt_and_activation_cannot_reuse_its_key`. Before the fix:
  - the genuine-retry assertions passed;
  - the reverse reuse raised `IdempotencyConflict` only through the gate-decision check, so the kind-specific match failed.
- A first draft of both tests used the fixed key `"K"`. The second test then failed with "used in a different project", because keys are globally unique and pytest shares one database per session. Both tests now use per-test unique keys.
- **Fix:** `_replay` takes `kind` and raises `IdempotencyConflict` when the stored operation kind differs.
- **After the fix:** both new tests pass. `uv run pytest` gave **59 passed, 0 failed, 0 skipped** (31.7 s).

Measured cost: CAD 0. No reservations.
Next action: Codex review of D042; the user commits and pushes.

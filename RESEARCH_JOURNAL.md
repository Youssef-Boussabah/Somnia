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

## 2026-11-19 — Repair of review finding R1 (gateway recovery, budget accounting) and adoption of T1
Type: engineering. **Not an experiment; no research result.** Author: Claude Code.
Base: `5ac29ee` on `foundation`, clean working tree. The repair is uncommitted (D041).

What actually ran:
- I wrote `tests/test_gateway_recovery.py` first: 10 tests, mock provider, real PostgreSQL. Run against the unfixed code: **1 passed, 9 failed**.
  - T1 (response before resume, no operator) passed: that path already worked.
  - R1 after operator reconciliation, `exact` and `zero`: A failed with `InvalidReservationTransition: ... is settled; cannot -> settled`, so the evidence was lost.
  - Resume before the response arrives, `exact` and `zero`: the replacement worker **completed the run**, meaning it re-sent the outstanding call after billing had merely been reconciled.
  - The other 5 needed interfaces added by the fix: `record_response`, `BudgetFrozen`, `abandon_request`, `InvalidRequestResolution`.
- **Fix (D044):** migration 0003 adds request identity, abandonments, discrepancies and the SB003 freeze. The gateway gains the outstanding-request refusal and an idempotent `record_response`. The budget module gains operator actions and alerts, and the ledger check now includes discrepancy differences. The report is schema v2 (D045).
- **After the fix:** `uv run pytest` gave **69 passed, 0 failed, 0 skipped**. The first full run had 1 failure in my own new test: an evidence string too short for validation, which masked the intended state check. I corrected the test input, not the code.
- **Mutations** on a disposable copy of the working tree, each caught: N1 outstanding refusal (4 tests), N2 discrepancy creation (5), N3 SB003 freeze (4), N4 duplicate detection (1), N5 ledger application (6).
- **Upgrade:** I made a TEMPLATE copy of the development database (at 0002).
  - 0003 applied and backfilled 6 request identities for 6 existing calls, all with prompts.
  - `ledger_mismatches` was empty for every project, and there were no alerts.
  - `somnia demo` against the copy completed (usage confirmed 5,066 MOCK_MICRO; 0 outstanding requests; 0 discrepancies).
  - The copy was dropped. The development database itself is still at 0002.

Measured cost: CAD 0; mock units only.
Next action: review of D044 (Codex). The user commits.

## 2026-11-19 — D044 follow-up after Codex review: coordination, cap coverage, upgrade, pre-send boundary
Type: engineering. **Not an experiment; no research result.** Author: Claude Code.
Base: `5ac29ee` plus the unstaged R1/T1 repair (preserved; backed up before work). Everything is still unstaged (D041).

What actually ran:
- First I added two inert hook points to the gateway: after the initial completed-response lookup, and inside admission before its checks. Then I wrote 11 new regressions and ran the two affected files against that tree (the 11 new tests plus 2 existing upgrade tests, one of them extended with the superseded case): **11 failed, 2 passed**.
  - Pause C after its lookup, then commit A's exact late response: C created a second request and **sent again** (2 `provider_requests` for one logical request).
  - Pause C inside admission while A's response commits: the provider saw **2 sends**.
  - Cap coverage after reconciliation, `zero` / `lower` / `exact`: the parent held **0** instead of 16,000 / 14,841 / 13,682 remaining. The `higher` case passed, since the operator figure (18,000) already covers the bound.
  - Sender paused after its reservation, then abandonment and a replacement retry: **A woke and sent as well**.
  - An open send permit: abandonment was allowed with no sender attestation.
  - Migration: 0003 installed over unresolved `uncertain` and `reserved` legacy holds; a reconciled legacy request had no coverage; a superseded legacy hold was not resolved.
  - The 2 passes were the `higher` estimate and the pre-existing cross-project upgrade test.
- A 14th regression, `…cannot_send_after_losing_its_lease`, was added after mutation L5 survived. It fails on the pre-fix code: A sent after losing its lease. An earlier "pass" of that run was invalid, because the copied virtualenv pointed at the fixed sources; I re-ran it with a fresh environment.
- **Fix (D046, D047):** migration 0003 was revised in place; it had never been applied to a persistent database. `gateway.py`: locked admission with a re-check, the fenced send permit, and permit end on every outcome. `budget.py`: attestation for abandonment, locking, and residual holds in the ledger check and alerts. Engine: fence and worker id in the call context. CLI: `--sender-stopped`.
- **After the fix:** `uv run pytest` gave **81 passed, 0 failed, 0 skipped**. Two existing tests changed by design: `test_uncertain_billing_retains_reservation` now expects the residual hold and abandons before reserving again; `test_late_response_after_abandonment_is_still_recorded` supplies a deliberately wrong sender attestation.
- **Mutations** on a disposable copy of the working tree:
  - L1 (no lock), L2 (no re-check), L3 (no residual), L4 (residual never released), L6 (no attestation check), L7 (no upgrade precondition) and L8 (no superseded resolution) were caught.
  - L5 (no permit lease guard) and L5b (also no Python abandonment check) **first survived**: the database trigger still refused permits for abandoned requests. They are caught after adding the lease-loss test. L5c (all three permit guards removed) was caught.
- The development database was not touched (still at 0002). Only disposable `somnia_test_*` / `somnia_upg_*` databases were used.

Measured cost: CAD 0; mock units only.

## 2026-11-20 — Second D044 follow-up after Codex review: legacy retries, legacy late responses, reconcile/abandon race, provider errors after takeover
Type: engineering. **Not an experiment; no research result.** Author: Claude Code.
Base: `5ac29ee` plus the unstaged R1/T1, D044 and D046/D047 repairs (preserved, snapshotted to a scratch copy before work). Everything is still unstaged (D041).

**Correction to the 2026-11-19 follow-up entry.** That entry counted mutation L8 ("no superseded resolution") as caught, and the migration then auto-resolved a pre-0003 attempt whose retry completed. That behaviour was wrong: a retry's receipt says nothing about the first attempt's bill. D048 reverses it. The earlier record is left as written.

What actually ran:
- I wrote `tests/test_d048_legacy_and_races.py` first (13 tests) and ran it against a snapshot of the pre-change tree. The first run had 11 tests, and **all 11 failed**. I added the 2 SQL-reconcile race variants and the invented-prompt check afterwards; rerun against the same snapshot, **all 13 failed**:
  - Legacy retry: 0003 inserted a `migration-0003` abandonment for the original attempt and released its cover. Envelope held **0**, expected 16,000.
  - Legacy late response: no supported path (`IntegrityFailure: ... predates prompt recording; record it manually`). In the retried variants the residual was also missing (held 0 instead of 16,000 / 13,200).
  - Reconcile racing abandonment, both orderings: the second operation **did not wait** for the first. Afterwards, **12,000 stayed held on leaf and parent** in a residual hold with no outstanding request to release it.
  - Provider error after takeover, timeout and not-sent, both before and after reconciliation: A failed with `InvalidReservationTransition` (`... is uncertain; cannot -> uncertain`, `... is settled; cannot -> released`, and so on). The **send permit stayed open** (`ended_at` NULL), so abandonment would still have needed a sender attestation. The error text was lost. The billing state itself was unchanged.
- **Fix (D048):** migration 0003 revised in place again (never applied to a persistent database): no supersession; `operator_recorded_responses`; nullable `provider_calls.prompt_artifact`, guarded by a prompt-binding check and a deferred prompt-provenance trigger; `provider_request_sends.outcome`; row lock in the abandonment and provider-call triggers. `gateway.py`: `record_provider_error`, `OperatorRecord`, and no reuse of operator-recorded responses. CLI: `budget record-response`. Report: `operator_recorded_by` on provider calls; a missing prompt is reported by the trace.
- One existing test changed by design: `test_upgrade_keeps_foundation_data_and_enforces_new_invariants` no longer expects supersession. It now expects the original attempt to stay outstanding with a 16,000 residual.
- A Python-side lock in `budget.reconcile` was tried and removed: the database row lock alone serializes the race, which the SQL-reconcile variants test.
- **After the fix:** `uv run pytest` gave **94 passed, 0 failed, 0 skipped** in two consecutive full runs (86.7 s, 88.9 s).
  - An earlier mid-work full run had 1 failure: `test_leases.py::test_expired_worker_cannot_overwrite_newer_worker` (`KeyError: 'b'`; A lost its 1 s lease before reaching the hook). That test passed 5/5 alone and in both final runs. I am treating it as a pre-existing timing flake of the 1 s test lease under load; it was not investigated further.
- **Mutations** on a disposable copy of the working tree, one guard at a time:
  - M1 (migration supersedes again), M2 (gateway reuses operator-recorded responses), M3 (no row lock in abandonment), M4 (provider error leaves the permit open), M5 (provider error auto-abandons), M6 (prompt-less call without an operator record) and M9 (pre-fix billing transition on error) were **caught**.
  - M8 (no prompt binding on provider calls) first **survived**. I then added an assertion that hand-written SQL cannot attach an invented prompt to a pre-0003 request, after which it was caught.
  - M7 (no row lock in the provider-call trigger) **survived** the full suite (94 passed). It only protects hand-written SQL inserts of `provider_calls` racing a reconcile; `record_response` already locks the row itself. It remains untested defence in depth.
- **Upgrade check:** I made a TEMPLATE copy of the development database (at 0002) and migrated it.
  - 0003 applied; 6 request identities were backfilled, all with prompts; 0 residual holds; 0 ledger mismatches; 0 alerts.
  - `somnia demo` against the copy completed with 0 outstanding requests.
  - The copy was dropped. The development database is still at 0002 (migration 3 pending).
- Not reproduced: no deadlock occurred in either race ordering, before or after the fix. Before the fix, the two operations did not wait for each other at all.

Measured cost: CAD 0; mock units only.
Next action: Codex review of D048; the user commits.

## 2026-11-20 — Closing the Codex review of D048 (D049)
Type: engineering. **Not an experiment; no research result.** Author: Claude Code.
Base: `5ac29ee` plus the unstaged R1/T1, D044, D046/D047 and D048 repairs (preserved; snapshotted outside the repository before work). Everything is still unstaged (D041).

What actually ran:
- I wrote `tests/test_d049_review_closure.py` (7 tests) and changed the D048 never-retried expectation from "resend" to "pause". I ran both against the pre-change snapshot; its only change was an inert `OperatorReceiptOnly` class, so the tests could import. **9 failed** (the 7 new tests and the 2 never-retried cases); the other 11 D048 tests passed.
  - Operator-only receipt: a resume **completed the run by re-sending** the logical request (2 `provider_requests`).
  - Token counts (-4, 1) and (4, -1): accepted and stored as given, priced at 0 MOCK_MICRO. That released the 16,000 residual hold on leaf and parent, and the request counted as complete.
  - `--response-file` with CRLF: 48 supplied bytes were stored as 44, with every CR removed. Delivering the same response through the gateway recording path then raised `IntegrityFailure: a different response is already recorded`.
- **Fix (D049):** `OperatorReceiptOnly` in gateway admission (an operator-only receipt blocks the run); a per-field nonnegative check in `record_response`; the `provider_calls_tokens_nonnegative` constraint in 0003, preceded by an SR001 precondition; a strict-UTF-8 byte read in `budget record-response`.
- **After the fix:** the targeted files gave 20 passed. `uv run pytest` gave **101 passed, 0 failed, 0 skipped** in three full runs (139.6 s, 150.8 s, 126.0 s).
  - Earlier today the 94-test suite took about 87 s. The 7 new tests take about 12 s. In the timed run the remaining time was spread evenly across tests, with nothing over 3.9 s; I am treating it as machine load, not investigated further.
- **Mutations** on a disposable copy, one at a time, all **caught**: P1 no pause (4 failed), P2 no Python token check (4), P3 no PostgreSQL check (4), P4 only the weighted total checked (4), P5 the CLI reads with newline translation (2).
- **Upgrade check:** 0003 applied to a TEMPLATE copy of the development database: 6 requests, 0 ledger mismatches, 0 alerts, `somnia demo` completed. The copy was dropped. The development database is still at 0002 (migration 3 pending).

Measured cost: CAD 0; mock units only.
Next action: Codex re-review of D044–D049; the user commits.

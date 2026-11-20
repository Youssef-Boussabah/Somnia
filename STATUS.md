# Somnia — current status
As of 20 November 2026 · Foundation (Assignment A) implemented and repaired after four reviews, mock-only · branch `foundation`

## Completed
**Documentation baseline v0.1** (unchanged import, now commit `63d608c`).

**Assignment A foundation: working infrastructure, exercised only with mocks.** Commits `d1a66f8`, `32bb93b`, `44d64be` and `b87707c` on the local branch `foundation`. The user re-created the history; earlier records cite the old hashes `1d195a6`, `e641d62`, `6704a22` and `4841892` for the first four commits:
- Local Git repository; remote `origin` = https://github.com/Youssef-Boussabah/Somnia, **not pushed**. Pushing is deferred to the review step and is done by the user.
- Reproducible environment: CPython 3.13.14 (uv-managed), psycopg 3.3.9, pydantic 2.14.3, pytest 9.2.0 and jsonschema 4.26.0 (dev), locked in `uv.lock`. PostgreSQL 17.12 + pgvector 0.8.7 via `docker-compose.yml`, pinned by tag and digest and bound to 127.0.0.1:5433.
- Migration 0001 (learner-side store). The database enforces:
  - immutability and budget counters;
  - reservation transitions;
  - gate/candidate hash matching;
  - development-only learning snapshots;
  - that a mock gate can never be recorded as independent.
- End-to-end mock cycle: record synthetic experience → snapshot → consolidate → scenarios → rehearse → extract → gate → activate → report. Retrieval in a fresh session returns the active version.
- Transactional budget reservations (`MOCK_MICRO` units, which are not money); fenced stage leases; resume; uncertain-billing hold; idempotent, audited activation; rollback; provenance trace; CLI.
- Public contracts for the evaluator in `contracts/` (JSON Schemas plus a README).

**Review repairs (19 Nov 2026), committed by the user as `b87707c`:**
1. Activation after rollback allocates a fresh set version under the project lock; the predecessor is the set that was actually active (D033).
2. Experience citations name the session (`s<n>/<key>`, `consolidate.v2`), so key reuse across sessions in one snapshot cannot misattribute evidence (D034).
3. Promotion requires the candidate, gate baseline and project to agree, and the active set to be exactly the validated baseline. A second candidate that passed against the same baseline is recorded as `stale_validation` and needs fresh validation (D032).
4. `SkillSetExport` v2 can be verified offline from its JSON plus a separately trusted digest. A stdlib-only verifier and the `skill-set verify` CLI command were added (D035).
5. The archived report describes the completed run (`completed`/`done`, report stage `succeeded`), and crash recovery is preserved (D036).
6. `GateVerdict` rejects mock+independent in the model and the JSON Schema; the DB CHECK is kept (D037).
- Plus migration `0002_activation_invariants`, which adds DB triggers for the same rules (D038). The development database was upgraded to 0002 when `somnia demo` ran.

**Repair after review of `b87707c` (19 Nov 2026), uncommitted in the working tree:**
- Activation/rollback idempotency replay is bound to the operation kind (D042).
- Before the fix, a rollback that reused an activation's key K, targeting that activation's set, returned the activation's old receipt: `applied=False`, set v1. Meanwhile v2 stayed active and no rollback was recorded.
- It now raises `IdempotencyConflict`. A genuine rollback retry (same key, same target) still returns the original rollback receipt.
- Handoff rule (D043): every Claude assignment ends with a written report and a review ZIP built outside the repository.

**Repair of review finding R1 at `5ac29ee` (19 Nov 2026), uncommitted in the working tree:**
- Billing reconciliation and request completion are now separate facts (D044, BUDGET.md).
- A late response is persisted exactly once, even after operator reconciliation.
- A replacement worker never re-sends a request that is still outstanding.
- A differing amount opens a billing discrepancy. The difference is applied once to leaf and ancestors, and the envelope tree is frozen until it is resolved.
- Evidence above a limit is kept and reported.
- Migration `0003_request_lifecycle`; report schema v2 (D045).
- The review's T1 probe and both R1 cases are now ordinary regression tests in `tests/test_gateway_recovery.py`.
- **Follow-up after the Codex review (D046, D047):**
  - one locked decision per logical request;
  - residual holds keep cap coverage while a reconciled request is outstanding;
  - fenced send permits stop a paused or fenced-out sender from sending after a replacement retried;
  - 0003 refuses to install over unresolved pre-0003 holds, with drain instructions.
  - Regressions are in `tests/test_d044_coordination.py` and `tests/test_migrations.py`.
- **Second follow-up after the Codex review (20 Nov 2026, D048):**
  - a pre-0003 attempt keeps its residual hold until its own evidence resolves it; a completed retry is reused but no longer supersedes it;
  - `somnia budget record-response` records a genuine late response with its usage. A pre-0003 request's prompt stays unrecorded (operator record, never reused as model output);
  - reconcile and abandonment serialize on the hold's row lock, so neither ordering strands a residual hold;
  - a provider error returning after a takeover ends the send permit with its outcome and leaves billing untouched; a timeout never authorizes a resend.
  - Regressions are in `tests/test_d048_legacy_and_races.py`.
- **Closing the D048 review (20 Nov 2026, D049):**
  - a logical request whose only receipt is operator-recorded pauses the run (`operator_recorded_response`): no reservation, no send, on every resume;
  - reported token counts must each be nonnegative, checked both in `record_response` and in PostgreSQL;
  - `--response-file` bytes are stored exactly, CRLF included.
  - Regressions are in `tests/test_d049_review_closure.py`.
- The development database is still at 0002. It will be upgraded by the next `somnia db migrate` or `somnia demo`; the upgrade was checked on a TEMPLATE copy.

## What is mock (not evidence of anything)
- Model responses, scenario validity checks, rehearsal outcomes, gate verdicts and paired scores are all scripted from labelled synthetic fixtures (`fixtures/development/`).
- No model was called and no generated code was executed.
- The embedding is non-semantic feature hashing.
- The mock gate is **not** independent validation. Skill sets activated through it carry `contains_mock_validation = true`, and the export verifier reports this as a scientific-use blocker.

## Checks (actual, 19–20 Nov 2026)
- After D049 (20 Nov), `uv run pytest` gave **101 passed, 0 failed, 0 skipped**: 94 earlier tests plus 7 in `test_d049_review_closure.py`. One existing D048 test now expects a pause instead of a resend.
  - On the pre-change tree, which needed only an inert error-class shim to import, all 7 new tests and the 2 updated never-retried cases failed on the defect.
  - Mutations P1–P5 (no pause, no Python token check, no PostgreSQL token check, weighted-total-only check, newline-translating read) were each caught.
  - 0003 on a TEMPLATE copy of the development database: applied, 0 ledger mismatches, demo completed; copy dropped. The development database is still at 0002.
- After the D048 follow-up (20 Nov), `uv run pytest` gave **94 passed, 0 failed, 0 skipped**, twice (86.7 s, 88.9 s): 81 earlier tests plus 13 in `test_d048_legacy_and_races.py`.
  - Against the pre-change tree, all 13 new tests failed on the defect itself.
  - Mutations M1–M9: 8 caught. M7, the row lock in the provider-call trigger, survived; it only covers hand-written SQL inserts, and no test exercises that path.
  - One mid-work full run failed `test_expired_worker_cannot_overwrite_newer_worker` (`KeyError: 'b'`). It then passed 5/5 alone and in both final runs. This is a timing flake of the 1 s test lease under load, not investigated further.
  - Upgrade check on a TEMPLATE copy of the development database: 0003 applied (6 requests backfilled, all with prompts, 0 residual holds), 0 ledger mismatches, `somnia demo` completed, copy dropped. The development database is still at 0002.
- After the D044 follow-up, `uv run pytest` gave **81 passed, 0 failed, 0 skipped** (details in RESEARCH_JOURNAL.md).
- After the R1 repair, `uv run pytest` gave **69 passed, 0 failed, 0 skipped**: 59 existing tests plus 10 in `test_gateway_recovery.py`.
  - Before the fix, 9 of the 10 new tests failed and T1 passed. Both R1 cases failed with `InvalidReservationTransition` (evidence lost). Both resume-before-response cases failed because the replacement **completed the run by re-sending** the outstanding call. The other 5 needed the new interfaces.
  - Mutations N1–N5 (outstanding-request refusal, discrepancy creation, spending freeze, duplicate detection, ledger application) were each caught.
  - Two existing tests changed by design: `test_crash_between_reserve_and_send_blocks_until_reconciled` now requires abandonment evidence before the retry, and `test_upgrade_keeps_foundation_data_and_enforces_new_invariants` builds its pre-0003 data without today's gateway. Two report assertions gained the new fields.
- After the `b87707c` repair, `uv run pytest` gave **59 passed, 0 failed, 0 skipped**, against a fresh real PostgreSQL + pgvector database per session.
  - Of the 2 new tests, the rollback-reusing-an-activation-key test was run first against the unfixed code: **DID NOT RAISE IdempotencyConflict**.
  - The rollback-retry test also failed before the fix, but only on its final assertion. The reverse reuse (an activation with a rollback's key) was already rejected, though only incidentally, as "used for a different gate decision", not because of the kind.
- Earlier repairs (`b87707c`): `uv run pytest` gave 57 passed, 0 failed, 0 skipped.
- The 12 new regression tests for findings 1, 2, 3, 5 and 6 were run against the unrepaired code first, and all 12 failed.
  - 8 failed on the defect itself: the unique-version collision; both candidates activated; cross-project activation accepted; evidence attributed to the wrong session; archived status `running`; and the model and schema accepting mock+independent.
  - 4 citation tests failed only because the old code had no `consolidate.v2` response.
  - The 5 export tests and 2 migration tests use interfaces added by the fix (verifier, `migrate(target=)`, migration 0002), so they could not run before it.
- `uv run somnia demo` completed all 7 stages. The upgrade from a foundation database was checked on a TEMPLATE copy of the development database: 0002 applied, and every existing set exported and verified.
- Details are in RESEARCH_JOURNAL.md.

## Not started / not implemented
- Real provider adapter and paid preflight; real embeddings.
- Sandboxed execution of generated code.
- Reference agent interface (needs joint definition with the evaluator).
- FastAPI/HTTP API; dashboard.
- Assignment B (independent evaluation).
- Separate learner/evaluator database roles. In local development a single superuser role is used.
- Evaluator-side retrieval for sealed tasks (required by D039, not built). The development retrieval path logs task queries to the learner store and must not be used for sealed tasks.
- Bounded automatic retries for failed stages are missing: a failed stage is simply retried by the next `run work`.
- Run manifests do not yet record the code commit, dirty-tree status or lock hash.

No results support an improvement claim. No benchmark tasks, oracles or sealed material exist in this repository.

## Locked constraints
Somnia; PostgreSQL + pgvector; custom memory layer; Claude Code core/backend; Codex evaluation/review; Claude frontend; no Higgsfield; CAD 50 additional model ceiling. **Repository: public on GitHub (user decision, D021)**. Sealed assessment material stays outside it. The user stages, commits and pushes (D041).

## Spending
Experimental model spending: **CAD 0**. No CAD envelopes exist. Outstanding reservations in the development database: **0**. Mock ledger use is in fake units only.

## Open blockers
| Item | Blocks |
|---|---|
| Experimental API access/model/pricing not verified | Real provider adapter, paid pilot, final study |
| Reference agent interface not agreed with evaluator | Real rehearsal/gate integration |
| Benchmarks/oracles not built (Assignment B) | Any meaningful learning result |
| Learner/evaluator credential isolation not implemented | Trustworthy final evaluation |
| Trusted channel for export digests not defined | Evaluator trusting a frozen set (D035) |
| No workflow to authorize a retry when the only receipt is operator-recorded (D049) | Such runs stay paused (`operator_recorded_response`) |
| An open send permit can only be closed by an operator's sender-stopped attestation (D046) | Fully automatic retry after a sender stalls mid-send; needs provider-side idempotency to remove |
| No lease heartbeat during a provider call (review U5) | Real providers slower than the lease TTL will routinely hit the D044 recovery path (safe, but operator-heavy) |
| Task sandbox not implemented | Executing generated code |

## Next gate
**Re-review of the D044–D049 repair** (Codex, Assignment D), then the user commits and pushes `foundation` to the public remote. In parallel, and only if authorized: Assignment B evaluation-contract work against `contracts/`. Paid work still requires the BUDGET.md preflight.

## Housekeeping
- `SHA256SUMS.txt` is the v0.1 documentation-delivery manifest and no longer matches the updated records; Git history is now the source of truth.
- `SOMNIA_PROJECT_KNOWLEDGE.md` is a generated reading copy and is now stale. The coordinator should regenerate it before re-uploading it to the ChatGPT Project.

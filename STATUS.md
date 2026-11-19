# Somnia — current status
As of 19 November 2026 · Foundation (Assignment A) implemented and repaired after review, mock-only · branch `foundation`

## Completed
**Documentation baseline v0.1** (unchanged import is commit `1d195a6`).

**Assignment A foundation: working infrastructure, exercised only with mocks.** Commits `e641d62`, `6704a22` and `4841892` on the local branch `foundation`:
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

**Review repairs (19 Nov 2026), uncommitted in the working tree for the user to review and commit:**
1. Activation after rollback allocates a fresh set version under the project lock; the predecessor is the set that was actually active (D033).
2. Experience citations name the session (`s<n>/<key>`, `consolidate.v2`), so key reuse across sessions in one snapshot cannot misattribute evidence (D034).
3. Promotion requires the candidate, gate baseline and project to agree, and the active set to be exactly the validated baseline. A second candidate that passed against the same baseline is recorded as `stale_validation` and needs fresh validation (D032).
4. `SkillSetExport` v2 can be verified offline from its JSON plus a separately trusted digest. A stdlib-only verifier and the `skill-set verify` CLI command were added (D035).
5. The archived report describes the completed run (`completed`/`done`, report stage `succeeded`), and crash recovery is preserved (D036).
6. `GateVerdict` rejects mock+independent in the model and the JSON Schema; the DB CHECK is kept (D037).
- Plus migration `0002_activation_invariants`, which adds DB triggers for the same rules (D038). The development database was upgraded to 0002 when `somnia demo` ran.

## What is mock (not evidence of anything)
- Model responses, scenario validity checks, rehearsal outcomes, gate verdicts and paired scores are all scripted from labelled synthetic fixtures (`fixtures/development/`).
- No model was called and no generated code was executed.
- The embedding is non-semantic feature hashing.
- The mock gate is **not** independent validation. Skill sets activated through it carry `contains_mock_validation = true`, and the export verifier reports this as a scientific-use blocker.

## Checks (actual, 19 Nov 2026)
- `uv run pytest`: **57 passed, 0 failed, 0 skipped**. The suite runs against a fresh real PostgreSQL + pgvector database per session.
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
| Task sandbox not implemented | Executing generated code |

## Next gate
**Re-review of the repairs** (Codex, Assignment D), then the user commits and pushes `foundation` to the public remote. In parallel, and only if authorized: Assignment B evaluation-contract work against `contracts/`. Paid work still requires the BUDGET.md preflight.

## Housekeeping
- `SHA256SUMS.txt` is the v0.1 documentation-delivery manifest and no longer matches the updated records; Git history is now the source of truth.
- `SOMNIA_PROJECT_KNOWLEDGE.md` is a generated reading copy and is now stale. The coordinator should regenerate it before re-uploading it to the ChatGPT Project.

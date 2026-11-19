# Somnia — current status
As of 18 November 2026 · Foundation (Assignment A) implemented, mock-only · branch `foundation`

## Completed
**Documentation baseline v0.1** (unchanged import is commit `1d195a6`).

**Assignment A foundation: working infrastructure exercised only with mocks.** Commits `e641d62` and `6704a22` on the local branch `foundation`:
- Local Git repository; remote `origin` = https://github.com/Youssef-Boussabah/Somnia (empty when checked; **not pushed**, pushing is deferred to the review step).
- Reproducible environment: CPython 3.13.14 (uv-managed), psycopg 3.3.9, pydantic 2.14.3, pytest 9.2.0, locked in `uv.lock`. PostgreSQL 17.12 + pgvector 0.8.7 via `docker-compose.yml`, pinned by tag and digest and bound to 127.0.0.1:5433.
- Migration 0001 (learner-side store). The database enforces immutability, budget counters, reservation transitions, gate/candidate hash matching, development-only learning snapshots, and that a mock gate can never be recorded as independent.
- End-to-end mock cycle: record synthetic experience → snapshot → consolidate → scenarios → rehearse → extract → gate → activate → report. Retrieval in a fresh session returns the active version.
- Transactional budget reservations (`MOCK_MICRO` units, which are not money), fenced stage leases, resume, uncertain-billing hold, idempotent and audited activation with stale-set/stale-parent rejection, rollback, provenance trace, CLI.
- Public contracts v1 for the evaluator in `contracts/` (JSON Schemas plus README with development examples).

## What is mock (not evidence of anything)
Model responses, scenario validity checks, rehearsal outcomes, gate verdicts and paired scores are scripted from labelled synthetic fixtures (`fixtures/development/`). No model was called. No generated code was executed. The embedding is non-semantic feature hashing. The mock gate is **not** independent validation. Skill sets activated through it carry `contains_mock_validation = true`.

## Checks (actual)
`uv run pytest`: **38 passed**, run against a fresh real PostgreSQL database per session, with no skips. Before the final lease test was added, three consecutive full runs (37 tests at that point) also passed. Mutation checks: 6 guards were deliberately broken one at a time. 5 were caught immediately. One survivor, the commit-time fence check, exposed a test gap. I fixed that with a new test plus a second guard, and the tests now fail when both commit guards are removed. Details are in RESEARCH_JOURNAL.md.

## Not started / not implemented
Real provider adapter and paid preflight; real embeddings; sandboxed execution of generated code; reference agent interface (needs joint definition with the evaluator); FastAPI/HTTP API; dashboard; Assignment B (independent evaluation); separate learner/evaluator database roles. In local development a single superuser role is used. Bounded automatic retries for failed stages are also missing: a failed stage is simply retried by the next `run work`. Run manifests do not yet record the code commit, dirty-tree status or lock hash.

No results support an improvement claim. No benchmark tasks, oracles or sealed material exist in this repository.

## Locked constraints
Somnia; PostgreSQL + pgvector; custom memory layer; Claude Code core/backend; Codex evaluation/review; Claude frontend; no Higgsfield; CAD 50 additional model ceiling. **Repository: public on GitHub (user decision, D021)**. Sealed assessment material stays outside it.

## Spending
Experimental model spending: **CAD 0**. No CAD envelopes exist. Outstanding reservations in the development database: **0**. Mock ledger use is in fake units only.

## Open blockers
| Item | Blocks |
|---|---|
| Experimental API access/model/pricing not verified | Real provider adapter, paid pilot, final study |
| Reference agent interface not agreed with evaluator | Real rehearsal/gate integration |
| Benchmarks/oracles not built (Assignment B) | Any meaningful learning result |
| Learner/evaluator credential isolation not implemented | Trustworthy final evaluation |
| Task sandbox not implemented | Executing generated code |

## Next gate
**Assignment D review** (Codex): an independent engineering/security review of the `foundation` branch before pushing to the public remote. In parallel, and only if authorized: Assignment B evaluation contract work against `contracts/v1`. Paid work still requires the BUDGET.md preflight.

## Housekeeping
`SHA256SUMS.txt` is the v0.1 documentation-delivery manifest and no longer matches the updated records. Git history is now the source of truth. `SOMNIA_PROJECT_KNOWLEDGE.md` is a generated reading copy and is now stale; the coordinator should regenerate it before re-uploading it to the ChatGPT Project.

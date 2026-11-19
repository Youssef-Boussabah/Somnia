# Somnia — decision log
Version 0.1 · Consolidated 18 November 2026

“Accepted” means recorded user direction. “Proposed” means an implementation/research default offered by this package. A decision can be revised with evidence and a dated superseding entry.

| ID | Status | Decision | Reason / consequence |
|---|---|---|---|
| D001 | Accepted | Name is Somnia | Earlier names are historical only |
| D002 | Accepted | Long-term research; initial 14-day sprint | Urgency with evidence-based milestones |
| D003 | Accepted | Additional model budget capped at 50 dollars | Earlier currency was CAD; keep conservative CAD interpretation |
| D004 | Accepted | PostgreSQL + pgvector from day one | One store for relational evidence, flexible records and retrieval |
| D005 | Accepted | Custom memory/learning layer | Research mechanisms remain under our control |
| D006 | Accepted | Claude Code leads core implementation | User explicitly swapped prior Claude/Codex roles |
| D007 | Accepted | Codex owns independent evaluation/review | Separate method construction from assessment |
| D008 | Accepted | Claude models own frontend; no Higgsfield now | User correction supersedes prior Codex/Higgsfield proposal |
| D009 | Accepted direction | Local execution; private repository initially | Limit cost; external repository not yet created |
| D010 | Accepted direction | Frozen-model learning; no weight updates in v0.1 | Learn through external memory and skill context |
| D011 | Accepted direction | Python/FastAPI, Inspect/pytest, NumPy/SciPy, Docker | Exact versions remain to be pinned |
| D012 | Accepted direction | Next.js/TypeScript, Three.js/R3F; SDK/MCP later | Contracts before visual polish and broad integration |
| D013 | Provisional | Voyage AI for embeddings | Verify access/pricing; keep adapter and versioned embedding spec |
| D014 | Proposed | One local Python reliability domain first | Objective verifiers and controlled interventions |
| D015 | Proposed | B3 skill optimizer vs B4 Somnia as primary contrast | Avoid only comparing against a weak stateless agent |
| D016 | Proposed | Separate development, promotion, pilot and final partitions | Gate reuse must not contaminate final measurement |
| D017 | Proposed | Exact vector search initially | Reproducible retrieval before index tuning |
| D018 | Proposed | Transactional reservations; routine spend envelope CAD 45 | Preserve hard-cap contingency and uncertain charges |
| D019 | Proposed | CLI/report path before full dashboard | Research must not depend on visualization completion |
| D020 | Proposed | Evidence-linked, applicability-scoped procedural skills | Test transfer and harmful generalization explicitly |

## Superseded or corrected
- SQLite-first recommendation: rejected; do not reintroduce it as an assumed shortcut.
- MongoDB mandatory because of hackathon sponsorship: no longer applicable.
- REVEIL/Reveil naming: superseded by Somnia.
- Fable 5.2 specifically assigned to dashboard: removed; Claude models generally own frontend.
- Codex as primary frontend engineer: superseded by user direction.
- Higgsfield assets in initial implementation: excluded.
- “AI sleeping” as a novel invention: unsupported; substantial prior art exists.
- Fresh-worktree replay as a confirmed competitor gap: unverified pending source audit.
- “ACE” proves a specific adversarial-test approach: ambiguous; resolve exact source.
- +5–10 pp, 90% recall, 5% regression and 100+ tasks as guaranteed outcomes: aspirations only.
- Earlier Day 1/completion indicators: planning text, not evidence implementation occurred.
- December 1 deadline: historical estimate based on a prior assumed start; use working-day schedule.
- Private repository initially (D009): superseded 2026-11-18 by the user's public GitHub repository (D021); sealed material remains outside it.

## Open decisions with resolution rule
| Question | Owner | Resolve by |
|---|---|---|
| Exact target/optimizer model and API access | User + coordinator | Official pricing/access preflight and small pilot |
| Python/dependency/image versions | Claude Code | Compatibility check and reproducible lock |
| Pinned primary comparator revision | Codex + coordinator | Source audit, license check and original run |
| Final task counts/repeats | Codex | Pilot feasibility/power analysis before final exposure |
| Exact schema and API implementation | Claude Code, reviewed by Codex | Preserve architecture invariants; version contract changes |
| Public release/license | User | After provenance/license and result review |
| Additional model/domain studies | User + coordinator | Evidence and remaining budget, never implicit extra spend |

Append entries with date, decision, evidence, alternatives, tradeoff and affected files. Changing an open proposal does not require ceremonial approval if it is routine and within authorized scope; changing explicit user constraints does.

## 2026-11-18 — Foundation implementation entries (Claude Code)
| ID | Status | Decision | Reason / consequence |
|---|---|---|---|
| D021 | Accepted (user) | The user created a **public** GitHub repository, https://github.com/Youssef-Boussabah/Somnia. This supersedes the "private repository initially" part of D009. | Code and project records may be published. Sealed assessment tasks, oracles, credentials and runtime traces stay outside this repository (`.gitignore` also excludes `sealed/`, `final_eval/`, `var/`, `.env`). Pushing is deferred to the Assignment D review. License/public-release terms remain the user's open decision. |
| D022 | Implemented | Python 3.13.14 (uv-managed), psycopg 3.3.9, pydantic 2.14.3, pytest 9.2.0; exact versions in `uv.lock`. FastAPI, NumPy/SciPy and Inspect are deferred until a component needs them. | Resolves the dependency question for the foundation scope only (D011 direction unchanged). |
| D023 | Implemented | PostgreSQL 17.12 + pgvector 0.8.7, image `pgvector/pgvector:0.8.7-pg17` pinned by digest `sha256:b513c485…bd8fd`, on localhost:5433. | Versions verified inside the image. Port 5432 was already in use on the host. |
| D024 | Implemented | Integrity rules live in the database: immutability triggers; budget counters maintained by triggers with guarded updates; allowed reservation transitions; gate/candidate hash match; `NOT (is_mock AND independent)`; development-only learning snapshots; no `final` partition value. | A bug in application code cannot bypass them. A superuser can still disable triggers, and hash-chain verification detects such edits. |
| D025 | Implemented | Two non-mixing ledger units: `CAD_MICRO` (money) and `MOCK_MICRO` (fake units). Mock providers only accept mock envelopes, and real providers would only accept CAD. | Lets mocks exercise the real reservation path without polluting real spend accounting. |
| D026 | Implemented | One expiring lease per run, using the DB clock and a monotonic fencing token. Each stage's outputs, attempt record and pointer move commit in one fenced transaction. Side-effecting calls (reservations, activation) re-check the lease in their own transaction and are idempotent by key. | At-least-once execution with idempotent writes (ARCHITECTURE.md), not exactly-once. |
| D027 | Implemented | `uncertain → released` is impossible. An uncertain reservation is resolved only by `reconcile` with an actual cost (possibly 0) and a note. A new worker marks a predecessor's held reservations uncertain, and any uncertain reservation blocks the run. Completed calls are reused by request key instead of being re-sent. | Implements BUDGET.md: never release funds just because no response arrived. |
| D028 | Implemented | Activation happens under a lock on the active-set pointer and requires: a gate `pass` on the re-verified content hash, the expected previous set, and the candidate's parent being the skill's active version. An idempotency key makes replays return the original result. Mock-gated activation needs `allow_mock_gate_activation` and marks the set `contains_mock_validation`. Rejections are audited. | Stale or duplicate promotion is impossible. Mock-validated sets are distinguishable by evaluators. |
| D029 | Implemented | Schema versioning uses forward-only SQL migrations with checksum verification, through a small custom runner rather than Alembic, since there is no ORM. A per-row `schema_version` exists only where JSONB payloads evolve (events, run config). Public contracts are versioned as JSON Schemas in `contracts/v1`. | Small, explicit and testable. Revisit if migrations become complex. |
| D030 | Implemented (provisional) | Retrieval v0 applies a domain/required-tags/exclusion-tags predicate, then exact cosine distance over a **mock, non-semantic** feature-hash embedding (`mock-hash-v1`, 64 dims), then packs into the skill-count and token ceilings. | A real embedding spec remains open (D013). Ranking quality is untested and meaningless with the mock embedding. |
| D031 | Proposed | The reference agent interface is not yet defined. Assignment A delivered the gate/retrieval/skill-set/execution contracts only. | It must be agreed jointly with the evaluator (HANDOFFS.md) rather than imposed by the learner side. |

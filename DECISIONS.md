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

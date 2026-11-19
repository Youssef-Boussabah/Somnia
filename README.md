# Somnia — project documentation
Version 0.1 · 18 November 2026 · Foundation implemented (mock-only); no research results. See [STATUS.md](STATUS.md).

Somnia investigates whether an agent can use downtime to discover weaknesses, run controlled experiments, and retain skills that improve later work. The base model stays frozen. Improvement must be demonstrated on tasks the learning system has never seen.

## Start here
1. Read [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) for the compact, authoritative handoff.
2. Read [PROJECT.md](PROJECT.md) for scope and [EXPERIMENTS.md](EXPERIMENTS.md) for what would count as evidence.
3. Use [AGENTS.md](AGENTS.md) and [HANDOFFS.md](HANDOFFS.md) when assigning engineering work.
4. Check [STATUS.md](STATUS.md) before acting. A plan is not a completed implementation.

## Developer quickstart (mock mode)
```
uv sync                                   # Python 3.13 + locked dependencies
cp .env.example .env                      # then set a local-only password
docker compose up -d --wait               # PostgreSQL 17 + pgvector 0.8.7 on localhost:5433
uv run somnia db migrate
uv run somnia demo                        # full mock cycle + report + fresh-session retrieval
uv run pytest                             # checks against a fresh real PostgreSQL database
```
Everything the demo produces is driven by synthetic fixtures and mock components. Public contracts for the evaluator are in [contracts/](contracts/README.md).

## Document map
| File | Owns |
|---|---|
| PROJECT.md | Mission, boundaries, acceptance criteria, responsibilities |
| RESEARCH.md | Source-backed prior art, hypotheses, novelty audit |
| ARCHITECTURE.md | Components, data model, contracts, security boundaries |
| MEMORY.md | Memory semantics, retrieval, revision and retirement |
| SLEEP_ENGINE.md | Resumable offline learning procedure |
| EXPERIMENTS.md | Splits, baselines, scoring, statistical analysis |
| BUDGET.md | CAD 50 ceiling, reservations, spending and model selection |
| DASHBOARD.md | Interface brief and truthful visual behavior |
| ROADMAP.md | Fourteen working-day sprint and milestone gates |
| AGENTS.md | Instructions for engineering agents |
| HANDOFFS.md | Ready-to-use scoped assignments |
| DECISIONS.md | Accepted decisions, proposals and superseded advice |
| RESEARCH_JOURNAL.md | Dated evidence and experiment entries |
| STATUS.md | Current state, blockers and next action |
| PROJECT_CONTEXT.md | Compact context for a fresh conversation |
| CHATGPT_PROJECT_INSTRUCTIONS.md | Text for the Project instructions field |

## Set up the ChatGPT Project
Copy the text in CHATGPT_PROJECT_INSTRUCTIONS.md into the Project instructions field. Add PROJECT_CONTEXT.md and SOMNIA_PROJECT_KNOWLEDGE.md as knowledge files. The latter is a generated reading copy of the specifications, including this index. Do not upload the ZIP as a substitute for readable knowledge files.

The ZIP contains the individual Markdown sources for the future repository, plus the instructions and compact context. Extract it, preserving filenames. The repository root is the intended home for these files; no repository has been created or modified by this documentation task.

The individual documents are the editing source of truth. Regenerate the combined reading copy after changes; do not edit both independently. Replace outdated Project attachments when publishing a new documentation version. ChatGPT context is not a guaranteed substitute for maintained files.

## Status and authority
“Accepted” records an explicit user decision or an already accepted direction. “Proposed v0.1” is a concrete engineering or research default, ready for review and implementation, not an assertion that the user separately approved every detail. “Verified” means checked against the cited primary material, not reproduced experimentally. “Unverified” means an open question.

Latest explicit user decisions supersede these files. Otherwise use DECISIONS.md for decisions, STATUS.md for progress, EXPERIMENTS.md for evaluation rules, and the specialist document for its domain. Resolve contradictions explicitly and update affected files. All timestamps are UTC unless labeled; planning dates use America/Toronto.

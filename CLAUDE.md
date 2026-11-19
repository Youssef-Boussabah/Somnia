# CLAUDE.md

Follow **AGENTS.md** — it is the authority for roles, constraints and research discipline.
Before working, read the current project records rather than relying on chat history:
PROJECT_CONTEXT.md, STATUS.md, DECISIONS.md, HANDOFFS.md, and the specialist file for your task
(ARCHITECTURE.md, MEMORY.md, SLEEP_ENGINE.md, BUDGET.md, EXPERIMENTS.md, DASHBOARD.md).

Working with the code (details in STATUS.md):
- Environment: `uv sync` (Python pinned in `.python-version`, deps locked in `uv.lock`).
- Database: `docker compose up -d --wait` (PostgreSQL 17 + pgvector, pinned by digest, localhost:5433);
  copy `.env.example` to `.env` with a local-only password. Never commit `.env`.
- Checks: `uv run pytest` (runs against a fresh real PostgreSQL database; no skips).
- Contracts: after changing `src/somnia/contracts.py`, run `uv run somnia contracts export` and record
  the contract change in DECISIONS.md.

Git: do not stage, commit or push; the user does. Leave changes unstaged and suggest a commit message
without co-author attribution (AGENTS.md).

Hard boundaries: no paid calls without the BUDGET.md preflight; never create or read sealed evaluation
material in this checkout; never write learned skills into the user's real agent configuration;
mock outputs and synthetic fixtures must stay labelled as such. Update STATUS.md and
RESEARCH_JOURNAL.md with actual outcomes after meaningful work.

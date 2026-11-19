# Somnia — scoped engineering handoffs
Version 0.1 · Ready for the implementation stage; these assignments have not been executed.

## Assignment A — Claude Code foundation
Read PROJECT_CONTEXT.md, STATUS.md, AGENTS.md, ARCHITECTURE.md, MEMORY.md, SLEEP_ENGINE.md and BUDGET.md.

Objective: implement the smallest durable end-to-end mock learning run. Use PostgreSQL + pgvector from the start. Establish the Python environment and dependency lock after checking actual compatibility. Record unresolved assumptions instead of silently changing accepted stack choices.

Own the core application, storage migrations, budget gateway, job state machine, typed contracts and internal tests. Define the reference agent interface jointly with the evaluator. Do not author or inspect the sealed final assessment.

Deliver: event/snapshot recording; mock model adapter; transactional reservations; stage leases and resumability; immutable candidate and gate-result records; idempotent staging/activation; a CLI command/report path. Use development fixtures with clearly fake outcomes.

Acceptance: an interrupted run resumes without duplicate promotion; budget cannot be oversubscribed by concurrent requests; unknown billing holds its reservation; stale parent versions cannot be promoted; artifacts trace to inputs. Run relevant tests and document actual outcomes.

No paid calls, new provider accounts, production deployment, public repository creation or real-agent skill adoption in this assignment. Return a report with files/commits, architecture choices, test evidence, failures and remaining work. Update project records.

## Assignment B — Codex independent evaluation
Read PROJECT_CONTEXT.md, STATUS.md, AGENTS.md, EXPERIMENTS.md, RESEARCH.md and BUDGET.md.

Objective: construct the evaluation contract and development pilot harness independently of the learning implementation. Own task manifests, reviewed oracles, mutation checks, paired scoring and result analysis. Audit and pin the proposed SkillOpt comparator, check its license and document any adaptation; do not claim reproduction before running it.

Prepare development and validation task specifications first. Plan sealed task generation and storage outside the learner-accessible checkout. Keep final task content/oracles out of reports to the learning agent. Freeze root-family split rules before generating variants.

Deliver: task schema; valid reference/known-fault fixtures; separate scoring boundary; condition configs; protocol manifest; analysis/report templates; leakage and invalid-result checks. Verify with mocks without generating scientific success claims.

Acceptance: hidden scoring checks remain unavailable to a solving agent; paired records cannot silently drop one condition; cluster/repeat IDs survive aggregation; synthetic wins are visibly marked; invalid or missing observations are reported. No paid calls until preflight is complete.

Return compatible public interfaces to Claude Code, with only development examples. Record which comparator capabilities are verified, unverified and adapted.

## Assignment C — Claude frontend
Begin after stable run/report contracts exist. Read DASHBOARD.md, ARCHITECTURE.md and AGENTS.md.

Objective: an accessible research interface showing a run, evidence and skill changes. Start with 2D views and labeled contract-valid fixtures. Add brain interactions only after real data integration is stable.

Own dashboard code and browser verification. Do not fabricate metrics, change backend semantics or inspect sealed evaluation items. No Higgsfield.

Acceptance: accurate units/denominators; clear empty/error/inconclusive states; keyboard/touch navigation; reduced-motion and narrow-screen support; no activation caused by hover; displayed data agrees with API fixtures/records. Report visual checks and remaining issues.

## Assignment D — Codex integration review
Review completed changes and contracts, not just a summary from the implementer. Check idempotency, schema constraints, permissions, sandbox boundaries, cost reservations, secret handling, reproducibility and experiment isolation. Verify that required tests were actually run.

Classify findings by impact and give reproducible evidence. Fix authorized scoped issues or return a concrete patch plan where ownership coordination is needed. Do not expand research scope to make a failing evaluation pass.

## Handoff report format
Objective and scope; repository/commit; files changed; contract changes; actual commands/checks and outcomes; scientific findings (if any); costs/reservations; unresolved issues; next gate.

The user can authorize independent work in parallel, but role definitions alone do not launch agents or guarantee isolation.

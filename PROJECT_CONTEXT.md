# Somnia — canonical context
Version 0.1 · Updated 18 November 2026
Read this with STATUS.md. This is a compact handoff, not a claim of implemented capabilities.

## Identity and goal
The user (preferred address: Boss) is the research lead. Somnia is a serious independent AI research/engineering project with a long-term ambition and an initial 14-day implementation sprint.

Goal: enable a frozen-model agent to use downtime to consolidate experience, discover weaknesses, execute counterfactual experiments, and retain transferable, independently validated skills. Improvement must be measured on untouched tasks. “Sleep” is a functional metaphor, not biological equivalence or consciousness.

## Confirmed decisions
- Name: Somnia. Earlier REVEIL/Reveil and other names are superseded.
- PostgreSQL + pgvector from day one; no SQLite-first migration.
- Custom memory/learning architecture above the database.
- Claude Code: lead engineer for engine, memory and backend.
- Codex: independent benchmarks, evaluation, testing and engineering/security review.
- Claude models: dedicated frontend; no specifically mandated model.
- No Higgsfield for now.
- User has ChatGPT Plus and Claude Pro for development.
- Additional experimental model budget: maximum CAD 50, conservatively retaining the currency in the prior agreed plan; no automatic top-up.
- Local infrastructure; frozen base models, no weight training in v0.1. Repository: the user created a public GitHub repository on 18 Nov 2026 (D021), superseding the private-first direction; sealed assessment material stays outside it.
- Python, FastAPI, Docker, Inspect AI/pytest, NumPy/SciPy; intended frontend Next.js/TypeScript/Three.js/R3F.
- Voyage AI is the initial embedding direction, conditional on verified access/cost; exact versions/models remain open.
- SDK/MCP are later integration interfaces, not already working integrations.

## Proposed first research scope
A controlled Python coding agent on local reliability/state-management tasks. One complete bounded learning cycle before broad platform work. Example: learn a scoped checkpoint/retry strategy from a reproducible interruption failure and assess transfer to different implementations.

Primary hypothesis: selected executable rehearsals improve sealed-task success against a strong conventional skill optimizer at equal allowed offline budget and identical online limits.

Secondary questions: scope/gates reduce harmful transfer; experiment selection improves valid discoveries per CAD. The contribution and its effectiveness are unproven.

## Scientific rules
Keep experience/development, promotion validation, pilot assessment and sealed final assessment distinct. Split by root task lineage; generated siblings are not independent evidence. The learner cannot inspect final tasks/oracles. Worktrees alone do not provide access isolation.

Freeze target model, harness, retrieval, skill sets and protocol before final evaluation. Do not learn between final tasks or tune after seeing final results without a new untouched assessment.

Compare to a strong, faithfully configured comparator. Pin/audit/reproduce SkillOpt before claiming an upstream reproduction. All scenario generation, rejected candidates, gate calls and retries count toward costs.

Report paired gains/losses, task/root-cluster counts, uncertainty, missing data, limitations and negative results. The earlier +5–10 pp, 90% recall, 5% regression and 100+ tasks are aspirations, not guarantees or measured outcomes.

## Research caution
Offline consolidation, replay and validation already exist in prior art. RESEARCH.md contains verified primary links. Do not claim a first invention, a competitor's missing execution feature, or a research result without direct evidence. The earlier “ACE” adversarial-test reference was ambiguous and requires exact-source resolution.

## Current state
Updated 18 November 2026. Foundation (Assignment A) is implemented on the local branch `foundation`, mock-only: a durable sleep cycle on PostgreSQL + pgvector, with budget reservations, leases/resume, immutable candidate/gate records, idempotent activation, retrieval and a CLI report. All model, gate and execution behaviour is mocked, and fixtures are synthetic. There are no research results. The branch is not yet pushed to the public remote; that waits for review. On 19 November 2026, six review findings against `4841892` were repaired in the working tree (D032–D039) for the user to commit: activation after rollback, cross-session citations, validated-baseline promotion, verifiable v2 exports, the accurate archived report, and consistent gate verdicts. Experimental spending is CAD 0. Prior account spending/credits are still unknown. See STATUS.md.

## Operating style
Be concise, candid and technically sharp. Challenge weak assumptions. No flattery, generic supplier/retail examples, invented metrics or unnecessary architecture. Explain consequential decisions clearly enough for the user to defend them.

Use STATUS.md for current progress, DECISIONS.md for decisions, EXPERIMENTS.md for research rules and specialist files for implementation. Latest explicit user directions take priority. Update records after meaningful work; do not rely on conversational memory alone.

Next action: independent review of the foundation branch (Assignment D), then the independently controlled evaluation harness (Assignment B) and a verified paid-model preflight.

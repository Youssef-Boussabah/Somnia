# Somnia — master specification
Version 0.1 · 18 November 2026

## Mission
Build a model-independent offline learning system that turns an agent's recorded experience into evidence-backed, reusable knowledge. Its scientific purpose is to test whether deliberately selected executable rehearsals improve future task performance beyond strong memory and skill-learning baselines at comparable cost.

“Sleep” is a scheduling and consolidation metaphor. Somnia does not claim consciousness, literal biological sleep, automatic weight learning, or general self-improvement.

## Confirmed direction
| Decision | Status |
|---|---|
| Name: Somnia | Accepted; supersedes REVEIL/Reveil and earlier working names |
| Serious long-term research, with an initial 14-day sprint | Accepted; results are not guaranteed |
| PostgreSQL + pgvector from day one | Accepted; no SQLite-to-Postgres detour |
| Custom memory and learning layer | Accepted |
| Claude Code leads engine/backend implementation | Accepted |
| Codex owns independent evaluation and engineering/security review | Accepted |
| Claude models build frontend | Accepted; no named model mandated |
| No Higgsfield for now | Accepted |
| Additional experimental model spending: at most CAD 50 | Recorded ceiling; currency inherited conservatively from agreed plan |
| Local infrastructure, private repository initially | Accepted direction; neither provisioned yet |

## First demonstrable capability
A controlled coding agent completes a task, leaves a replayable record, and later enters one bounded sleep run. Somnia identifies a weakness, creates a valid experiment, executes it in an isolated environment, proposes an applicability-scoped skill, and either stages it after an independent gate or rejects it with evidence. A new agent session retrieves a fixed, versioned skill set. A separate evaluator tests whether that set helps on untouched tasks.

A run with no accepted skill is valid. “The pipeline ran” and “the agent improved” are different outcomes.

## Concrete example: interrupted research indexing
An agent implements a local indexer for a research notebook. The task looks successful on small files. Its trace suggests the indexer advances a progress checkpoint before writing results.

During sleep, Somnia asks: “Could a crash between these operations silently lose indexed content?” A fixture injects a failure at that boundary, restarts the indexer, and checks that the final index contains each intended record exactly once. It also checks a known-correct reference implementation and an intentionally faulty one.

A proposed lesson might be: “For resumable work, make completed output durable before advancing progress; make a repeated attempt safe.” Its scope must distinguish a local transaction from an external side effect that cannot be rolled back. It must not prescribe a single global lock for every concurrent program.

An independent gate tests the lesson on different implementations and incompatible cases. The sealed final suite uses new tasks and hidden checks. This is an illustrative mechanism, not a completed experiment or a benchmark item to reuse in the final test.

## v0.1 scope
Required: a controlled reference agent; frozen task manifests; durable PostgreSQL records; episodic evidence; versioned procedural skills; bounded retrieval; executable rehearsals; deterministic correctness checks; independent promotion validation; conservative cost accounting; resumable jobs; immutable reports and rollback.

First task domain: local Python reliability/state-management problems with controlled fault injection. Introduce concurrency only where scheduling can be made reproducible. Initial families and counts are proposals in EXPERIMENTS.md.

A minimal dashboard may display completed runs and provenance once contracts are stable. The research workflow must work from a CLI/report without the dashboard.

Deferred: weight updates; unrestricted autonomous agents; browser/web tasks; a general-purpose task marketplace; a hosted multi-user product; a dedicated graph database; broad native agent integrations; production scheduling; large cinematic assets; an elaborate 3D brain before the end-to-end loop works.

## Architecture commitments
Python engine and state machine; PostgreSQL/pgvector; FastAPI at the service boundary; Docker-based restricted execution; Inspect AI plus pytest for evaluation; NumPy/SciPy for analysis; Next.js/TypeScript with Three.js/React Three Fiber for the intended frontend. SDK/MCP are integration targets after the controlled reference agent.

Voyage AI is the initial embedding direction, conditional on verified access, cost and a pinned embedding specification. Provider adapters must allow a different embedding service without changing memory semantics. Do not silently mix embedding spaces. Exact dependency versions remain to be resolved against the development environment and locked before execution.

## Milestone acceptance
| Gate | Evidence required |
|---|---|
| Foundation | Frozen development manifests, documented task oracles, database migrations, bounded mock calls |
| First sleep cycle | A fully traceable candidate accepted or rejected, resume after interruption, no duplicate promotion |
| Scientific pilot | Matched baseline comparisons, valid paired records, cost and uncertainty report |
| v0.1 research prototype | Reproducible artifacts, sealed-test policy followed, limitations and negative findings documented |

The previous +5–10 percentage-point improvement, at least 90% context recall, at most 5% regressions and 100+ tasks are aspirations, not demonstrated facts or mandatory success claims. Budget and power determine study size. Recall is secondary and requires a separately specified ground truth.

## Responsibilities
The user is research lead and final decision-maker. This ChatGPT Project coordinates research, architecture, critique and continuity. Claude Code implements the learning method. Codex constructs and operates independent evaluations and reviews implementation. A dedicated Claude frontend session builds the interface. Development assistants are not automatically the experimental models.

Evaluation independence requires access restrictions and frozen artifacts, not just two model brands or two Git worktrees.

## Definition of completion for this task
A consistent documentation package and reusable Project context. No product code, external repository, paid experiment, model purchase or configured ChatGPT Project is implied by delivering these files.

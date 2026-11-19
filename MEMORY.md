# Somnia — memory architecture
Version 0.1 · Proposed v0.1 design

## What memory means
Memory is persisted information made available to a future model invocation through retrieval or context injection. The base model's weights do not change. The cognitive labels below describe function; they are not claims of biological equivalence.

| Memory class | Content | v0.1 treatment |
|---|---|---|
| Working | Current task, constraints, selected context | Bounded session state; disposable derived view |
| Episodic | Actions, observations, failures, outcomes | Immutable events plus replayable references |
| Semantic | Source-supported facts and environment properties | Versioned facts; uncertain claims remain labeled |
| Procedural | Strategies with conditions and evidence | Primary learning artifact; independent gate required |
| Evaluative | Test outcomes, contradictions, uncertainty | Immutable evidence, separate from skill prose |
| Prospective | Open hypotheses and deferred experiments | Ranked queue; no assumption a hypothesis is true |

Implement these as a small set of shared records with typed semantics, not six separate storage systems. v0.1 prioritizes episodic evidence, procedural skills and evaluative provenance.

## Memory record
Required: project/scope; type; immutable version ID; body; source event/artifact IDs; origin (observed, user-provided, inferred, synthetic); validity status; applicable environment; recorded time; superseded version; sensitivity; supporting and contradicting evidence IDs.

A confidence label is an estimate with provenance, not a calibrated probability unless calibration has been measured. Synthetic experience must never become a historical fact about a user or a real environment.

## Skill record
Required: short title; actionable procedure; applicability predicate; exclusions; prerequisites; supporting experiments; known counterexamples; candidate hash; validator/suite version; target model/harness evaluated; decision; version; token length.

Illustrative skill: “Commit progress only after durable output, and make retries safe.” Scope: resumable local work with a controlled persistence boundary. Exclusion: irreversible external effects without an idempotency contract. Evidence and validation fields are mandatory before promotion; this example has neither and remains illustrative.

## Lifecycle
observed/inferred memory → candidate → validated/staged → active → superseded or retired.

Rejected and inconclusive are separate terminal candidate outcomes. “Staged” means the gate passed; “active” means selected for use. Controlled research can automatically select staged candidates under a frozen protocol. Exporting into a person's live Claude/Codex instructions requires explicit adoption.

Retirement removes a version from default retrieval, not from the evidence history. A rollback switches the active skill-set pointer to a known prior set. Contradictory evidence remains linked.

## Consolidation
1. Preserve source events unchanged.
2. Cluster related episodes using explicit keys and, when configured, semantic retrieval.
3. Produce a summary with citations to individual episodes.
4. Distinguish repeated observations from independent evidence; duplicate traces do not strengthen a claim.
5. Resolve contradictions by source authority, scope and temporal validity. A newer timestamp alone does not establish truth.
6. Propose knowledge changes; do not overwrite active skills as part of summarization.

For disagreement that cannot be resolved from evidence, mark both claims disputed and queue an experiment. Do not invent a compromise.

## Retrieval
Apply permission/project filters, lifecycle validity and applicability before ranking. Query exact keys and full-text candidates; optionally add vector candidates from the same embedding specification. Fuse rankings with a deterministic method (reciprocal-rank fusion is a proposed default), deduplicate versions, and pack into a token budget.

Use exact vector search initially. Add approximate indexes only if measured latency requires them and record recall changes. Record the full eligible/selected version IDs, retrieval configuration and rejection reasons for evaluation.

Proposed initial cap: at most three procedural skills and 1,500 total memory tokens per target invocation, enforced within the common overall context limit. Tune on development data only. All learning conditions get the same allowable memory budget; a no-memory control leaves that allowance unused.

Never compare “Somnia with retrieval and extra context” to a baseline arbitrarily denied the same interface. Freeze retrieval configuration for the primary comparison.

## Embeddings
Store provider, model/revision, dimensions, normalization, source-content hash and generated time. Choose one specification after checking access and budget. Never compare vectors from incompatible models. Re-embedding produces a new versioned index with a deliberate activation step; it does not silently mutate an experiment.

## Protection and quality
Treat transcripts and generated memories as untrusted content. They cannot override tool permissions, evaluation access, budget controls or system instructions. Secrets and identifiers should be removed before model transmission. Store only information needed for the authorized project.

Track grounded factual precision, recall on labeled memory queries, contradiction resolution, retrieval relevance, prompt overhead, task performance and regressions separately. The earlier 90% recall aspiration is not meaningful until query sets and scoring rules are defined.

For deletion requests, remove affected raw content, derivatives and caches according to a documented policy; tombstone provenance without retaining deleted private text. Artifact references are not an excuse to keep sensitive copies indefinitely.

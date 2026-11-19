# Somnia — research specification and evidence register
Version 0.1 · Primary-source review: 18 November 2026
Status: initial literature map, not an exhaustive novelty review or reproduction.

## Research question
Under a fixed offline spending envelope and an identical online agent harness, does selecting and executing counterfactual experiments produce more transferable procedural knowledge than strong experience-based skill optimization?

The contribution under investigation is a combination of experiment selection, executable evidence and applicability-aware learning. None is presumed novel on its own.

## Hypotheses
H1 (primary): Somnia improves macro-averaged task success on a sealed evaluation set relative to the strongest feasible, faithfully configured skill-learning comparator, with equal allowed offline budget and identical online limits.

H2 (secondary): Applicability conditions and an independent validation gate reduce harmful transfer under controlled environment changes, compared with a prespecified gate/scope ablation.

H3 (exploratory): A simple experiment-priority rule discovers more valid, actionable failures per CAD than random selection from the same candidate pool.

H1 can fail even if Somnia discovers more failures. H3 can succeed without helping later tasks. Report these separately. No weight update is part of these hypotheses.

## Primary sources checked
| ID | Source and link | Verified scope; implication |
|---|---|---|
| R1 | [Microsoft SkillOpt repository](https://github.com/microsoft/SkillOpt) | Text-space skill optimization for frozen agents; candidate primary comparator. Source reproduction and commit pinning remain undone. |
| R2 | [SkillOpt-Sleep documentation](https://microsoft.github.io/SkillOpt/docs/sleep/) | Offline session review, replay, consolidation, validation and staged adoption already exist. Optional variants and repeated rollouts are documented. Somnia cannot claim these general ideas as new. |
| R3 | [Anthropic: The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory) | Published 6 November 2026. Discusses organizing memory through dreaming, correcting notes and creating representations. Establishes substantial overlap with consolidation. |
| R4 | [Letta: Sleep-time Compute](https://www.letta.com/blog/sleep-time-compute/) | Published 21 April 2025. Describes using idle computation to rewrite memory. A relevant foundation, not evidence of Somnia's effectiveness. |
| R5 | [Agentic Context Engineering, arXiv:2510.04618](https://arxiv.org/abs/2510.04618) | Describes evolving context playbooks through generation, reflection and curation. Evaluate as a context-learning comparator where feasible. |
| T1 | [PostgreSQL JSON types](https://www.postgresql.org/docs/current/datatype-json.html) | JSONB supports indexed structured-document storage alongside relational records. |
| T2 | [pgvector](https://github.com/pgvector/pgvector) | Supports exact vector search and approximate indexes; exact search is the initial research default. |
| T3 | [Inspect sandboxing](https://inspect.aisi.org.uk/sandboxing.html) | Documents sandbox abstractions for executing evaluation tools and code. Our isolation settings still need implementation and testing. |

These are living sources. Record exact commit IDs for code and a dated source snapshot or content hash when beginning a reproduction. Source pages establish documented capabilities, not measured parity under our harness. Competitor results must not be copied into Somnia's results.

## Corrections to earlier discussion
Do not assert that SkillOpt is purely prompt replay or incapable of environment execution. Backend capabilities vary and must be audited at a fixed commit. The earlier proposed fresh-worktree gap is unverified.

“ACE” is ambiguous. R5 is Agentic Context Engineering; it does not by itself substantiate the earlier assertion about a particular adversarial unit-test generation system. Resolve the exact paper before making that claim.

A database recommendation is an architectural judgment, not a universal ranking. Compatibility through a Python interface or MCP does not mean Somnia is already integrated into Claude/Codex or that every framework shares memory semantics.

No broad “first AI that sleeps,” “first validated skill learner,” or “big tech cannot do this” statement is defensible from this review.

## Prior-art audit before a novelty claim
For each candidate competitor, capture: title/authors, exact version, license, available code, training data access, optimizer/target models, environment execution, scenario generation, selection strategy, skill representation, validation protocol, total cost and integration limitations.

For SkillOpt, run its documented example at a pinned revision before adaptation; compare its published/default behavior to any adapted harness. Log each deviation. Inspect replay isolation, generated scenario handling, validation split usage, external tool support and cost coverage. Do not disable strong documented options simply to create an easy baseline.

Expand literature work to active failure discovery, adversarial test generation, curriculum/active learning, software repair, episodic replay, procedural memory and continual adaptation. Each proposed novelty needs a mechanism-level comparison and a direct primary source. Neuroscience can motivate a metaphor; it cannot validate an engineering mechanism.

## Candidate contribution statement
Proposed, not established: “Somnia studies budgeted selection of executable counterfactual rehearsals for frozen coding agents, with evidence-linked skills and applicability-aware validation.”

A credible first report can still be valuable if it finds that simpler replay performs equally well, or that selection overhead overwhelms its benefits. Bound the claim to the measured model, harness, task distribution and budget.

## Reproduction record template
Source ID; URL; retrieved date; commit/release; license; environment digest; installation notes; exact command; model IDs; dataset hashes; documented expected behavior; observed behavior; discrepancies; adaptation diff; cost; artifacts; conclusion.

No competitor implementation has been cloned, run, or reproduced during documentation preparation.

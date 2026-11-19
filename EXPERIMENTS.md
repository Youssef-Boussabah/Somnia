# Somnia — experimental protocol
Version 0.1 · Proposed preregistration draft, not a completed study
Owner: independent evaluation agent (Codex), with user review.

## EXP-001: Does executable rehearsal improve later reliability?
Unit of learning: a versioned skill set derived from a fixed experience corpus.
Primary contrast: Somnia (B4) minus the strongest feasible conventional skill-learning comparator (B3).
Target: one frozen model/version and one fixed coding-agent scaffold.
Domain: controlled local Python reliability tasks. No external services.

The primary outcome is task success under a deterministic oracle. A better-looking explanation is not success.

## Task design
Proposed initial mechanisms: restart/checkpoint consistency; retry/idempotency; atomic updates and controlled concurrent access. Use independent root task designs, not merely renamed copies of the same function.

Each case needs a contract, allowed tools, dependencies, resource limits, visible smoke checks, hidden scoring checks, reference implementation and known-fault mutations. Deterministic scheduling and injected failures are preferred to timing-sensitive race tests.

The indexer example in PROJECT.md is public development material and must not appear in the sealed final set. Transfer should change implementation structure and assumptions, not just identifiers. Environment-change cases should include situations where a learned strategy is inapplicable.

## Data separation
| Partition | Proposed initial size | Who can access it | Purpose |
|---|---:|---|---|
| Experience/development | 24 tasks | Learner and implementer | Build traces, debug, learn |
| Promotion validation | 16 tasks plus fixed retention/exclusion checks | Gate runner; learner sees bounded decisions | Stage candidates |
| Pilot assessment | 20 tasks | Evaluator; results may inform development | Estimate variance, cost and design problems |
| Sealed final assessment | Target 60 tasks; 100+ only if affordable and justified | Evaluator only until method freeze and scoring | Final primary comparison |

Counts are planning parameters, not assets that already exist. Freeze actual counts, root-family grouping, repetitions, exclusion rules and model identifiers before paid execution. Final sample size is set from pilot cost and power analysis before looking at final results. If a useful study does not fit the cap, publish a limited pilot.

Split by root task/template and causal scenario lineage before generating variants. Keep all descendants in one partition. Record hashes and lineage IDs; check near duplicates manually and programmatically. A different random seed does not guarantee an independent task. Synthetic tasks can be contaminated through shared generators or explicit examples.

Maintain a manifest of sealed items without exposing content to learner processes. Keep hidden oracles inaccessible to the target agent while it solves the task. Worktrees are organizational tools, not access control.

## Conditions
| ID | Condition | Purpose |
|---|---|---|
| B0 | Stateless agent | Establish raw task difficulty |
| B1 | Episodic retrieval, no offline skill optimization | Separate retrieval from learning |
| B2 | Reflection-derived skill text | Test whether simple reflection is enough |
| B3 | Strong skill optimizer with rehearsal/validation | Primary competitive baseline |
| B4 | Somnia: selected executable rehearsal plus scoped skills | Proposed method |

B3 should use pinned SkillOpt/SkillOpt-Sleep where adaptation is feasible. First reproduce the original documented behavior, then document adapter changes. A local reimplementation must be labeled “SkillOpt-inspired,” never a reproduced upstream baseline.

Do not limit B3 to textual replay merely to give B4 exclusive access to executable tools. Match interfaces and report where upstream behavior differs. B3's rehearsal, optimizer, memory budget and validation options must be chosen on development/pilot data, not final scores.

For feasibility, first run B0 and B3/B4 on the pilot; add B1/B2 and ablations within the remaining allowance. The final primary test requires B3 and B4. If B3 cannot be reproduced/adapted faithfully, narrow the claim and name the actual comparator.

## Fairness and compute
Use the same target model, online prompt scaffold, task tools, output limits, step limits, per-task compute ceilings, retrieval interface and permitted skill-context size across comparable conditions. Keep optimizer-model access consistent for B3/B4.

Match allowed offline CAD envelopes. Report actual consumption, input/output tokens, calls, attempts, wall time and rejected work. Equal spending limits do not imply equal consumed computation, and tokens across different models are not equivalent compute.

Include preparation, scenario generation, validity checks, optimization and gate calls in learning cost. Count online inference and embedding cost separately and in the total. Data-generation/reproduction cost still counts against our real project ceiling.

A test-time-compute control (spending saved offline budget on extra online attempts) is valuable if affordable, but exploratory unless preregistered. Report amortized cost at several prespecified deployment task counts; do not assume unlimited future reuse.

## Freeze procedure
Before final assessment, record:
- Data and oracle hashes, counts, clusters and exclusions.
- Target and optimizer IDs/settings, full prompts, budgets and seeds.
- Skill-set hashes for every condition; retrieval configuration.
- Primary metric/contrast, confidence-interval method and stopping policy.
- Infrastructure retry policy; treatment of timeouts/missing observations.
- Versioned code, dependency lock and sandbox image digests.

The final evaluator reads these frozen artifacts. No learning between final tasks; each task begins from a fresh context and the same frozen condition skill set. Randomize/interleave condition order to reduce provider/time effects.

After aggregate final results are revealed, tuning ends for this evaluation round. Any subsequent changed method needs a new untouched final set; old results become development evidence. Never select a checkpoint by its final-test score.

## Scoring and analysis
Task success = all required correctness assertions pass within common quotas, without forbidden actions. Record individual assertions but use one task-level outcome for the primary metric.

Primary score: macro-average success across the prespecified mechanism strata. Primary effect: paired difference between B4 and B3 in percentage points. Report sample size, independent root-design count, repeats, gains (B3 fails/B4 passes) and losses (B3 passes/B4 fails).

Report a 95% interval for the paired difference using a prespecified stratified cluster bootstrap resampling independent root designs and retaining their descendants/repeats together. For genuinely independent single-trial binary pairs, an exact McNemar test is a useful supplementary check. Do not treat repeated attempts or generated siblings as independent samples.

Use pilot observations to assess whether the desired effect can be resolved. A 5–10 percentage-point aspiration is not evidence that 60 or 100 tasks provide adequate power. Small or unbalanced clusters can make intervals unreliable; explicitly report this and avoid a confirmatory claim.

The primary claim is limited to one contrast. H2/H3 and per-stratum/subgroup analyses are exploratory unless separately preregistered and powered. Do not search many variants and report the best unadjusted p-value.

## Secondary metrics
- Regression rate: tasks B3 solves and B4 fails, divided by tasks B3 solves. Undefined if denominator is zero; show numerator/denominator.
- Gross gains and losses, not just net gain.
- Valid failure discoveries per CAD, with invalid scenario cost included.
- Cost per solved task and amortized learning cost.
- Skill retrieval coverage, applicability errors, context overhead.
- Retention across successive learning cycles on reserved development/validation panels.
- Memory factual precision/recall only on separately labeled queries.

## Gate versus final evaluation
Promotion validation can reject a bad candidate. It cannot prove no regression on the task population. Its queries and repeated use are logged and limited. A candidate may pass the gate and fail the final study.

## Error policy
Agent-caused timeout, malformed solution and exceeded tool quota count as task failures. Infrastructure failures use a frozen retry policy: at most one technical retry with the same configuration, budget permitting. If one paired side is invalid due to infrastructure, mark the pair incomplete and report it; do not treat that as a win or silently discard it.

Include a missing-data sensitivity summary counting incomplete outcomes pessimistically/optimistically. Provider alias/model changes invalidate comparability and require a new study ID. Never retry selectively because an otherwise valid answer was wrong.

## Publication criteria
A positive primary result needs a positive effect with uncertainty supporting the claimed direction, a faithful comparator, intact isolation, complete accounting and reproducible artifacts. A prototype release may instead report an inconclusive or negative result.

No automatic “publishable” designation follows from passing an engineering test suite. Report every planned condition, deviations, failed candidates and stopped runs. Final evaluation data exposure must be disclosed.

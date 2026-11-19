# Somnia — budget and model policy
Version 0.1 · Hard project limit: CAD 50 in additional experimental model spending

The user said “max 50$, nothing more.” The preceding plan used CAD, so this package conservatively records CAD 50. Do not reinterpret the ceiling as USD or add an automatic top-up.

Existing ChatGPT Plus/Claude Pro subscriptions support development under their own terms and limits. Do not assume they supply API credits or authorize unattended experimental use through unofficial routes. No paid call has been made by this documentation task.

## Accounting boundary
Count all paid model/embedding usage for this research: pilot, comparator reproduction, task/scenario generation, target attempts, optimization, judging, gate checks, final evaluation, retries and failed calls if billed. Include applicable conversion costs, taxes and fees in the CAD ceiling. No new paid database, hosting or tooling is planned.

Existing promotional credits may reduce cash expense only after expiry, eligible endpoints and remaining balance are verified. Track the credit consumption and equivalent list-price cost for scientific comparisons. Never assume the old hackathon credits are usable.

## Proposed envelopes
| Purpose | Maximum allocation (CAD) |
|---|---:|
| Access checks and pilot calibration | 5 |
| Baseline reproduction/adaptation | 8 |
| Somnia development experiments | 12 |
| Frozen final primary comparison | 15 |
| Measured reruns/limited ablations | 5 |
| Uncommitted billing/FX contingency | 5 |
| Total | 50 |

Routine planned work stops at CAD 45; contingency is not an automatic experiment allowance. Reallocate unused envelopes with a recorded reason while preserving the ceiling. Protect final-evaluation funds before spending on extra hypotheses or dashboard models through paid APIs.

This is an allocation, not a price quote or proof the study fits. Shrink experiment breadth if necessary. Do not spend all funds optimizing and leave no independent evaluation.

## Before selecting an experimental model
Check current official model identifier, availability, exact input/output/cached/reasoning charges where applicable, supported tool/schema features, context/output ceilings, usage reporting, rate limits, retention terms and available prepaid/hard-spend controls.

Candidate selection is based on measured cost per valid completed task and an informative pilot difficulty range. Choose one affordable, stable target and freeze it. The optimizer may be the same model. A second provider/model is optional replication, not required for v0.1.

Exact model choice and prices remain unresolved deliberately: no current price was checked or model purchased in this task. Do not write fictional model IDs into configuration. Local models may be evaluated if the laptop can run them reliably, but measure latency and quality rather than assuming they are free substitutes.

## Conservative call reservation
Every provider call passes through one budget gateway. Store integer micro-CAD units, never floating-point currency totals.

Before sending a request, determine an upper bound for input tokens, all billable output/reasoning tokens, fees and currency conversion. Reserve that upper-bound cost atomically against project, phase and run envelopes. Reject if confirmed spend + outstanding reservations + new reservation would exceed the relevant ceiling.

Use maximum permitted output and conservative full input rates; do not rely on unconfirmed cache discounts. A call whose billable categories cannot be bounded must not start under this policy. Retries need fresh reservations.

After completion, reconcile reported usage and retain request ID plus pricing snapshot. A timeout or crash with uncertain charges keeps its reservation outstanding until reconciled; never release funds merely because no response arrived. Parallel workers share the same transactional ledger.

A local ledger is not a provider-enforced billing guarantee. Combine it with prepaid/no-auto-reload or verified hard account limits where available, low concurrency and a CAD buffer. If reliable bounds or account controls are unavailable, pause paid execution and continue mock/local work.

## Pilot-to-study estimate
Let c be a conservative observed cost per complete target attempt, k the required conditions, n the task count and r the repeats. Estimate online cost as n × k × r × c, then add measured offline learning, embeddings, infrastructure retries and billing contingency. Use a high-cost pilot estimate rather than the cheapest run.

Choose n and r before final results exist. If the estimate exceeds the protected envelope, reduce secondary conditions first, then breadth/repeats with explicit limitations. Do not weaken hidden checks or omit rejected-run costs.

## Spend ledger template
Date; study/run; purpose; provider/model; request ID; pricing/FX snapshot; reserved micro-CAD; actual tokens/charges; credits used; confirmed cash cost; unresolved liability; remaining project balance; artifact references.

Initial state: no expenditure by this documentation task. Prior account balances and charges are unknown and must be reconciled before the first paid run.

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

## Billing reconciliation and request completion are separate facts (D044)
A provider call has two independent records:
- **billing**: what the reservation cost (`reserved -> settled | released | uncertain -> settled`);
- **request**: whether a response exists. The request identity and prompt are recorded with the reservation, before the send. The request completes when its response is recorded, and is resolved without a response only by an evidence-backed abandonment.

**An operator-entered amount, including zero, does not by itself authorize another send.** `budget reconcile` settles billing only. Until the request has a recorded response or an abandonment, any later attempt of the same request stops with `request_outstanding` instead of sending.

**A late response is always recorded, exactly once.** This holds even if the worker's lease has expired or an operator has already reconciled. The record includes the request identity, response, provider request id and reported usage.
- **The worker that lost its lease** records the evidence but still cannot advance its stage.
- **A duplicate delivery** of the same response changes nothing.
- **A different response** for the same request is refused.

Outcomes by billing state when the response arrives:
| Hold state | Effect |
|---|---|
| `reserved` | Settled with the reported usage (the normal path). |
| `uncertain` | Reconciled with the reported usage. The hold was orphaned while the call was in flight. |
| `settled` by an operator, **same amount** | Nothing to adjust. The response is reused, with no second charge. |
| `settled` by an operator with a **different amount**, or `released` | Both records are kept and a **billing discrepancy** is opened (operator amount, reported amount, difference). See below. |

**What a billing discrepancy does:**
- The difference is applied to the leaf envelope and every ancestor **once**.
- New reservations anywhere in that envelope tree are refused with `billing_discrepancy` until an operator resolves it.
- Resolving it (`budget resolve-discrepancy`) acknowledges the discrepancy and lifts the freeze. It never applies the difference again.

**One decision per logical request (D046).** A logical request is every send attempt of the same request (same run and request key). These decisions all take the same per-request database lock:
- *reuse, block or reserve*;
- granting a send permit;
- recording a response;
- abandoning the request.

So a response that commits while a replacement is deciding is either seen and reused, or waits until the replacement has decided. It can never slip between two checks.

**Reconciliation and abandonment serialize on the hold (D048).** The abandonment check locks the hold's reservation row before deciding. A concurrent `budget reconcile`, or a plain SQL update of that row, is therefore either seen by the abandonment, which then releases the residual hold the reconciliation created, or it waits and sees the abandonment, so no residual is created. Before this, either ordering could leave a residual hold that nothing would ever release.

**The cap still covers a reconciled request that may yet bill (D046).** Reconciling an uncertain hold whose request is still outstanding settles the operator's figure X. It also keeps the remaining possible charge `max(bound − X, 0)` held as a *residual hold* on the leaf and every ancestor. A sibling therefore cannot use money the outstanding request can still consume.
- The residual is released exactly once, when the response is recorded (the reported usage then counts once, with any difference as a discrepancy) or when the request is abandoned.
- Provider-reported usage above the bound is still recorded as an overrun.

**Evidence is never discarded to keep counters within a cap.** If recorded usage puts confirmed spend above a limit, the ledger shows the overrun, `budget_alerts` reports it, and no new spending fits.

### Send permits and the pre-send boundary (D046)
A sender must take a **send permit** immediately before contacting the provider. The permit is fenced by its lease and refused for an abandoned request, under the logical-request lock. The permit is marked *ended* when the provider call returns, whatever the outcome.
- **No permit:** provably nothing was sent. A sender paused before its permit, whose lease expired or whose request was abandoned, can never obtain one later. That is what stops it waking up and sending after a replacement retried.
- **Permit open:** the sender may be paused between its permit and the network write, or the request may be in flight. A provider log showing no request *yet* cannot establish that this sender will never send. Local checks cannot close that last gap; only provider-side idempotency could.

**A provider error after a takeover (D048).** When the sender's call returns with an error, the permit is ended with its outcome: `not_sent` (a definitive rejection before acceptance) or `outcome_unknown` (timeout, dropped connection). The error text is kept in `end_note`. The billing record is changed only while the hold is still `reserved`, because the sender still owns it: `not_sent` releases it and `outcome_unknown` marks it uncertain. If a successor has already marked the hold uncertain, or an operator has reconciled it, the billing state, amount and residual hold are left exactly as they are. The sender then reports `LeaseLost`.
- The error never resolves the request. A replacement still stops with `request_outstanding`.
- **A timeout alone never authorizes another send.** The operator abandons with provider-side evidence. Because the permit has ended, no sender-stopped attestation is needed. A recorded `not_sent` outcome can be cited, but the abandonment is still an operator decision.

### Retrying a request whose response never arrives
Only `budget abandon-request <reservation> --evidence ... --actor ... [--sender-stopped ...]` permits a retry without a response. It is allowed only:
- after the hold has been marked uncertain (or reconciled);
- never while the hold is `reserved`, because its owner may be alive;
- never once a response is recorded.

What it requires depends on the send permit:
| Permit state | Required |
|---|---|
| none | the reason, e.g. "no send permit was granted". Safe, because nothing was sent and the sender is fenced out |
| ended (the call returned, for example with an unknown outcome) | provider-side evidence that the request failed or never completed |
| **open** | provider-side evidence **and** `--sender-stopped`: an attestation that the sender can no longer send, e.g. its worker process is confirmed terminated. The database cannot verify it |

Acceptable provider-side evidence is specific to the request:
- the provider's request log or dashboard showing no request with this request id or idempotency key in the relevant window;
- a provider error or status record showing this request failed or was cancelled;
- written confirmation from provider support.

Elapsed time, a usage export that does not yet show the call, or the absence of a local response are **not** sufficient on their own. Billing for the hold is reconciled separately, from billing evidence.

If the original response arrives after abandonment, it is still recorded, and a discrepancy opens if its usage differs from the reconciled amount. The duplicate spend is then visible rather than hidden.

### Upgrading a database to 0003
Migration 0003 refuses to install if a pre-0003 gateway hold is still `reserved` or `uncertain` without a recorded response, because its sender may still be running the old code. The error lists the holds. To proceed:
1. stop every `somnia run work` process and anything else running the pre-0003 code;
2. for a hold still `reserved`: wait until its run lease has expired, then run `somnia run work <run>` once with the pre-0003 code, which marks the hold uncertain and blocks the run;
3. reconcile each uncertain hold from billing evidence (`somnia budget reconcile`);
4. re-run `somnia db migrate`.

After the upgrade, each such request is outstanding, with no resend and its remaining charge held, until its response is recorded or it is abandoned with evidence plus `--sender-stopped`. Pre-0003 sends have no permits, so their sender state is recorded as unknown.

**A completed retry does not resolve the original attempt (D048).** If a pre-0003 reconciled hold's logical request later completed under another hold (a pre-0003 retry), the retry's response stays reusable: a later call of the same request reuses it without a reservation or a send. But that receipt says nothing about what the first attempt billed. So the first attempt stays outstanding, with its remaining possible charge held, until its own evidence resolves it: its own response, or an abandonment.

### Recording a late response that reached the operator (D048)
`somnia budget record-response <reservation> --response-file F --input-tokens N --output-tokens N --request-id ID --actor ... --evidence ...` records a genuine response the gateway never received. An example is a pre-0003 request whose worker is gone, with its response and usage retrieved from the provider. It applies the same billing rules as the gateway, exactly once:
- a matching operator figure needs no adjustment;
- a different figure opens a discrepancy, and the difference is applied once;
- the residual hold is released;
- a second delivery of the same response changes nothing;
- a different response is refused.
- each reported token count must be nonnegative on its own; zero is valid (D049). A negative count is refused before anything is written, even if the weighted total would be zero or positive. PostgreSQL enforces the same rule.
- the response file is stored byte for byte: strict UTF-8, CRLF kept (D049). The same response delivered later by the original sender then matches, and returns the existing receipt with no second charge.

What it records, and what it does not:
- **No prompt is invented.** For a request created before 0003, the prompt was never stored, so the call keeps `prompt_artifact` NULL. `operator_recorded_responses` holds the actor, the evidence and `prompt_recorded = false`. The database refuses a call without a prompt unless it has that operator record.
- **Operator-recorded responses are never reused as model output.** They settle billing and complete the request, but the response's link to the request rests on the operator's matching. A later call of the same request reuses the gateway's own receipt if there is one, for example a retry's.
- **If the only receipt is operator-recorded, the run pauses** (D049): it is blocked with `operator_recorded_response: ...`. Billing and provenance are kept. Every resume pauses again, with no reservation and no send. Authorizing a retry for such a request is a workflow that has not been built yet.

## Pilot-to-study estimate
Let c be a conservative observed cost per complete target attempt, k the required conditions, n the task count and r the repeats. Estimate online cost as n × k × r × c, then add measured offline learning, embeddings, infrastructure retries and billing contingency. Use a high-cost pilot estimate rather than the cheapest run.

Choose n and r before final results exist. If the estimate exceeds the protected envelope, reduce secondary conditions first, then breadth/repeats with explicit limitations. Do not weaken hidden checks or omit rejected-run costs.

## Spend ledger template
Date; study/run; purpose; provider/model; request ID; pricing/FX snapshot; reserved micro-CAD; actual tokens/charges; credits used; confirmed cash cost; unresolved liability; remaining project balance; artifact references.

Initial state: no expenditure by this documentation task. Prior account balances and charges are unknown and must be reconciled before the first paid run.

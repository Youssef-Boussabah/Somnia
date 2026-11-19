# Somnia — Brain Dashboard brief
Version 0.1 · Frontend owner: a dedicated Claude session

## Purpose
Make a sleep run understandable: what experience triggered it, what was tested, what the evidence showed, what knowledge changed and how much it cost.

The desired identity is a calm research instrument with an interactive brain. Avoid generic KPI cards claiming intelligence gains. The brain represents functional modules, not anatomically verified cognition.

## Interaction
Hover or keyboard-focus a region to reveal a labeled extension with restrained curved connecting lines and a compact panel. Selection opens detailed evidence. Touch uses tap; there is no hover-only navigation.

Suggested functional regions: experiences, consolidation, hypotheses, experiments, skills and validation. Labels describe software responsibilities. Activity is driven by real backend stage events.

## Core views
| View | Required content |
|---|---|
| Run overview | Run/study ID, state, configuration, progress, spending/reservations, termination reason |
| Evidence timeline | Ordered events with timestamps, sources and links to artifacts |
| Experiment detail | Hypothesis, intervention, oracle validity, attempts, failures and resource usage |
| Skill detail | Before/after text, scope, exclusions, evidence, gate decision, version history |
| Evaluation | Baseline/method scores, n, independent clusters, uncertainty, gross gains/losses and costs |
| Budget | Confirmed spend, unresolved reservations, remaining allowance and limit source |

No data is different from zero. Inconclusive is different from failure. A passing promotion gate is different from improved final performance. Display units and denominators beside metrics.

## Implementation stages
1. Accessible 2D run/evidence views using documented API contracts.
2. Contract-valid labeled fixtures for UI development, clearly marked “simulated.”
3. Connect real completed-run data and persisted progress.
4. Add interactive Three.js/React Three Fiber brain once performance and navigation work.

Use Next.js and TypeScript. Animation can be implemented in code; Higgsfield is excluded. Do not add a library solely to animate a decorative connector. Respect reduced-motion settings, provide a 2D fallback, and keep text readable with keyboard access and sufficient contrast.

Do not invent an overall “brain health,” “IQ” or “learning percentage.” Any metric needs a definition, data source and calculation. An unfinished run must not show a fabricated uplift.

## Review and acceptance
Inspect desktop and narrow layouts in a browser; test keyboard navigation, long evidence text, empty/partial/error states, disconnected progress streams and reduced motion. Verify that displayed values match API records. The dashboard must expose provenance without exposing secrets or sealed evaluation tasks.

A visual prototype can be built from fixtures after the API contract is stable. It must not block the first scientific experiment.

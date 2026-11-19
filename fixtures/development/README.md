# SYNTHETIC DEVELOPMENT FIXTURES — NOT RESEARCH DATA

Everything in this directory is hand-written fake material for exercising Somnia's
infrastructure. None of it is an observation, a model output, a measurement or a benchmark item.

| File | What it is |
|---|---|
| `experience_indexer_checkpoint.json` | A fake development session based on the public illustrative indexer example in PROJECT.md. Recorded with `origin='synthetic'`. |
| `mock_scripts/indexer_checkpoint_v1.json` | Scripted responses for the mock provider, mock scenario validity checks, mock rehearsal outcomes and mock gate verdicts for a first cycle. |
| `mock_scripts/indexer_checkpoint_revision_v1.json` | The same for a cycle that revises the first skill (used for stale-parent checks). |
| `mock_scripts/two_passing_candidates_v1.json` | A first cycle in which two different candidates both pass the mock gate against the same baseline (used for stale-validation checks, D032). |
| `run_config.mock.json` | Run configuration: mock provider, `MOCK_MICRO` pricing (fake units, not money), limits, mock-gate activation allowed. |

Rules:
- Mock consolidate responses (`consolidate.v2`) cite events as `s<session ordinal>/<key>` in `source_event_refs` (D034); all scripts here cite session 1 of their snapshot.
- Paired scores in mock gate verdicts are scripted and stored with `synthetic: true`.
- The indexer example is public development material and must never be used in the sealed final set (EXPERIMENTS.md).
- No sealed, validation or pilot task content belongs here.

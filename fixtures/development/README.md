# SYNTHETIC DEVELOPMENT FIXTURES — NOT RESEARCH DATA

Everything in this directory is hand-written fake material for exercising Somnia's
infrastructure. None of it is an observation, a model output, a measurement or a benchmark item.

| File | What it is |
|---|---|
| `experience_indexer_checkpoint.json` | A fake development session based on the public illustrative indexer example in PROJECT.md. Recorded with `origin='synthetic'`. |
| `mock_scripts/indexer_checkpoint_v1.json` | Scripted responses for the mock provider, mock scenario validity checks, mock rehearsal outcomes and mock gate verdicts for a first cycle. |
| `mock_scripts/indexer_checkpoint_revision_v1.json` | The same for a cycle that revises the first skill (used for stale-parent checks). |
| `run_config.mock.json` | Run configuration: mock provider, `MOCK_MICRO` pricing (fake units, not money), limits, mock-gate activation allowed. |

Rules:
- Paired scores in mock gate verdicts are scripted and stored with `synthetic: true`.
- The indexer example is public development material and must never be used in the sealed final set (EXPERIMENTS.md).
- No sealed, validation or pilot task content belongs here.

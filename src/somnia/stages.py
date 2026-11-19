"""Ordered sleep-cycle stages (SLEEP_ENGINE.md). Snapshot + envelope are fixed at run creation."""

STAGES: tuple[str, ...] = ("consolidate", "scenarios", "rehearse", "extract", "gate", "activate", "report")
DONE = "done"


def next_stage(stage: str) -> str:
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else DONE

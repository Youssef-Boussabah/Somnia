"""Typed domain errors and translation of Somnia's custom PostgreSQL SQLSTATEs."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg


class SomniaError(Exception):
    """Base class for all Somnia domain errors."""


class ImmutableRecordError(SomniaError):
    """An UPDATE/DELETE/TRUNCATE was attempted on an immutable record (SQLSTATE SM001)."""


class BudgetExceeded(SomniaError):
    """A reservation does not fit in the envelope or one of its ancestors (SQLSTATE SB001)."""


class InvalidReservationTransition(SomniaError):
    """A reservation state change is not allowed (SQLSTATE SB002)."""


class GateHashMismatch(SomniaError):
    """A gate decision does not reference the exact candidate content (SQLSTATE SG001)."""


class PartitionViolation(SomniaError):
    """Non-development experience was offered to a learning snapshot (SQLSTATE SP001)."""


class IdempotencyConflict(SomniaError):
    """An idempotency key was reused with different content."""


class SequenceError(SomniaError):
    """An event sequence number is not the next contiguous number for its session."""


class LeaseUnavailable(SomniaError):
    """The run is leased by another live worker, or is not in a claimable state."""


class LeaseLost(SomniaError):
    """This worker's lease expired or was superseded; its writes were rejected."""


class BillingUncertain(SomniaError):
    """A provider call may have been billed; its reservation is retained until reconciled."""


class RunBlocked(SomniaError):
    """The run cannot proceed until an operator resolves the recorded reason."""


class StaleActivation(SomniaError):
    """The active skill set changed since the candidate's baseline (optimistic check failed)."""


class StaleParentVersion(SomniaError):
    """The candidate revises a skill version that is no longer the active one."""


class IneligibleCandidate(SomniaError):
    """The candidate's gate decision does not permit activation under the given policy."""


class IntegrityFailure(SomniaError):
    """A stored hash does not match recomputed content."""


class FixtureError(SomniaError):
    """A development fixture or mock script is malformed or not labelled synthetic."""


_SQLSTATE_MAP: dict[str, type[SomniaError]] = {
    "SM001": ImmutableRecordError,
    "SB001": BudgetExceeded,
    "SB002": InvalidReservationTransition,
    "SG001": GateHashMismatch,
    "SP001": PartitionViolation,
}


def translate(exc: BaseException) -> BaseException:
    """Return a domain error for Somnia SQLSTATEs, otherwise the original exception."""
    if isinstance(exc, psycopg.Error):
        state = getattr(exc, "sqlstate", None)
        cls = _SQLSTATE_MAP.get(state or "")
        if cls is not None:
            diag = getattr(exc, "diag", None)
            msg = getattr(diag, "message_primary", None) or str(exc)
            err = cls(msg)
            err.__cause__ = exc
            return err
    return exc


@contextmanager
def translating() -> Iterator[None]:
    """Re-raise Somnia SQLSTATE errors as typed domain errors."""
    try:
        yield
    except psycopg.Error as exc:
        translated = translate(exc)
        if translated is exc:
            raise
        raise translated from exc

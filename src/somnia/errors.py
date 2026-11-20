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


class OperatorActionRequired(SomniaError):
    """The sleep engine must block the run until an operator acts; ``reason_code`` prefixes the block reason."""

    reason_code = "operator_action_required"


class BillingUncertain(OperatorActionRequired):
    """A provider call may have been billed; its reservation is retained until reconciled."""

    reason_code = "uncertain_billing"


class RequestOutstanding(OperatorActionRequired):
    """An earlier send of the same request has neither a recorded response nor an evidence-backed
    abandonment. Billing reconciliation alone does not resolve it, so the request is not sent again."""

    reason_code = "request_outstanding"


class OperatorReceiptOnly(OperatorActionRequired):
    """The only receipt for this logical request was recorded by an operator. It keeps its billing and
    provenance, but is not reused as model output and does not authorize another send (D049): the run pauses."""

    reason_code = "operator_recorded_response"


class BudgetFrozen(OperatorActionRequired):
    """An open billing discrepancy freezes new spending in the envelope tree (SQLSTATE SB003)."""

    reason_code = "billing_discrepancy"


class InvalidRequestResolution(SomniaError):
    """A request abandonment or evidence record is not allowed in the current state (SQLSTATE SR001)."""


class RunBlocked(SomniaError):
    """The run cannot proceed until an operator resolves the recorded reason."""


class StaleActivation(SomniaError):
    """The active skill set changed since the candidate's baseline (optimistic check failed)."""


class StaleValidation(StaleActivation):
    """The gate verdict was produced against a skill set that is no longer active; validate again."""


class StaleParentVersion(SomniaError):
    """The candidate revises a skill version that is no longer the active one."""


class IneligibleCandidate(SomniaError):
    """The candidate's gate decision does not permit activation under the given policy."""


class ProjectMismatch(IneligibleCandidate):
    """The candidate, its gate baseline and the target project do not all belong to one project."""


class IntegrityFailure(SomniaError):
    """A stored hash does not match recomputed content."""


class ActivationInvariantViolation(IntegrityFailure):
    """The database rejected a skill-set membership or activation record (SQLSTATE SA001)."""


class FixtureError(SomniaError):
    """A development fixture or mock script is malformed or not labelled synthetic."""


_SQLSTATE_MAP: dict[str, type[SomniaError]] = {
    "SM001": ImmutableRecordError,
    "SB001": BudgetExceeded,
    "SB002": InvalidReservationTransition,
    "SG001": GateHashMismatch,
    "SP001": PartitionViolation,
    "SA001": ActivationInvariantViolation,
    "SB003": BudgetFrozen,
    "SR001": InvalidRequestResolution,
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

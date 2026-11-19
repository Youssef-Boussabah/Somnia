"""Pluggable component interfaces.

Only mock implementations exist in this assignment (``somnia.mock``). The independent
evaluator (Codex) is expected to implement ``GateValidator`` against its own validation
suites, outside the learner's process and credentials, and return a ``GateVerdict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .contracts import ExecutionResult, GateRequest, GateVerdict, ScenarioSpec


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    request_id: str


class ProviderNotSent(Exception):
    """The provider definitively did not accept the request (safe to release the reservation)."""


class ProviderOutcomeUnknown(Exception):
    """The request may have been accepted and billed (timeout, dropped connection, crash)."""


@runtime_checkable
class ModelProvider(Protocol):
    name: str
    is_mock: bool
    unit: str  # "CAD_MICRO" for real providers, "MOCK_MICRO" for mocks

    def count_tokens(self, text: str) -> int: ...

    def complete(self, *, model: str, template_id: str, prompt: str, max_output_tokens: int,
                 request_key: str) -> ProviderResponse: ...


@runtime_checkable
class GateValidator(Protocol):
    name: str
    version: str
    independent: bool
    is_mock: bool

    def validate(self, request: GateRequest) -> GateVerdict: ...


@runtime_checkable
class ScenarioExecutor(Protocol):
    name: str
    is_mock: bool

    def execute(self, scenario: ScenarioSpec, *, scenario_key: str, attempt_no: int) -> ExecutionResult: ...

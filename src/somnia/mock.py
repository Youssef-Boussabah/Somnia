"""Deterministic mock components for the foundation cycle.

NOTHING HERE IS A MODEL, A VALIDATOR OR A MEASUREMENT.
  * MockProvider returns scripted fixture text; token counts use a chars/4 approximation.
  * MockGate returns scripted verdicts, always ``is_mock=True, independent=False``.
  * MockExecutor never executes code; it returns scripted outcomes.
  * mock_embedding is feature hashing of word tokens: deterministic, not semantic.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .contracts import ExecutionResult, GateRequest, GateVerdict, PairedScore, ScenarioSpec
from .errors import FixtureError
from .hashing import hash_obj, sha256_text
from .interfaces import ProviderNotSent, ProviderOutcomeUnknown, ProviderResponse

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "development"
MOCK_EMBEDDING_SPEC = {"id": "mock-hash-v1", "provider": "mock", "model": "feature-hash", "revision": "1",
                       "dims": 64, "normalized": True, "is_mock": True}


def load_mock_script(name: str, root: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise FixtureError(f"invalid mock script name {name!r}")
    path = (root or FIXTURE_ROOT) / "mock_scripts" / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("synthetic") is not True or "SYNTHETIC" not in data.get("label", ""):
        raise FixtureError(f"{path}: mock scripts must be explicitly labelled synthetic")
    if data.get("script_id") != name:
        raise FixtureError(f"{path}: script_id does not match file name")
    return data


def approx_tokens(text: str) -> int:
    """Mock tokenizer (ceil(chars/4)). Real adapters must use the provider's own counting."""
    return max(1, math.ceil(len(text) / 4))


class MockProvider:
    name = "mock"
    is_mock = True
    unit = "MOCK_MICRO"

    def __init__(self, script: dict[str, Any], *, fail_mode: str | None = None):
        self.script = script
        self.fail_mode = fail_mode  # None | "not_sent" | "unknown_outcome" (test hooks)
        self.calls: list[str] = []

    def count_tokens(self, text: str) -> int:
        return approx_tokens(text)

    def complete(self, *, model: str, template_id: str, prompt: str, max_output_tokens: int,
                 request_key: str) -> ProviderResponse:
        self.calls.append(request_key)
        if self.fail_mode == "not_sent":
            raise ProviderNotSent("mock: request rejected before acceptance")
        if self.fail_mode == "unknown_outcome":
            raise ProviderOutcomeUnknown("mock: connection dropped after send; billing unknown")
        try:
            body = self.script["responses"][template_id]
        except KeyError as exc:
            raise FixtureError(f"mock script has no response for template {template_id!r}") from exc
        text = json.dumps(body, sort_keys=True, ensure_ascii=False)
        out_tokens = approx_tokens(text)
        if out_tokens > max_output_tokens:
            raise FixtureError("scripted mock response exceeds max_output_tokens")
        return ProviderResponse(text=text, input_tokens=approx_tokens(prompt), output_tokens=out_tokens,
                                request_id=f"mock-{request_key[:20]}")


class MockGate:
    """Scripted gate. Clearly NOT independent scientific validation."""

    name = "somnia.mock_gate"
    version = "0.1.0"
    independent = False
    is_mock = True

    def __init__(self, script: dict[str, Any]):
        self.verdicts = script.get("gate_verdicts", {})
        self.suite_hash = hash_obj(self.verdicts)

    def validate(self, request: GateRequest) -> GateVerdict:
        scripted = self.verdicts.get(request.candidate.body.slug)
        if scripted is None:
            decision, reason, scores = "inconclusive", "no scripted verdict for this candidate", []
        else:
            decision, reason = scripted["decision"], scripted["reason"]
            scores = [PairedScore(**s, synthetic=True) for s in scripted.get("paired_scores", [])]
        return GateVerdict(candidate_hash=request.candidate.content_hash, decision=decision,
                           reason=f"MOCK GATE (not independent validation): {reason}", validator=self.name,
                           validator_version=self.version, suite_id=request.suite_id, suite_hash=self.suite_hash,
                           independent=False, is_mock=True, paired_scores=scores,
                           evidence_refs=[f"mock_script:gate_verdicts/{request.candidate.body.slug}"])


class MockExecutor:
    """Returns scripted rehearsal outcomes. Executes no generated code."""

    name = "somnia.mock_executor"
    is_mock = True

    def __init__(self, script: dict[str, Any]):
        self.outcomes = script.get("rehearsal_outcomes", {})

    def execute(self, scenario: ScenarioSpec, *, scenario_key: str, attempt_no: int) -> ExecutionResult:
        seq = self.outcomes.get(scenario_key, [])
        outcome = seq[attempt_no - 1] if attempt_no <= len(seq) else "infrastructure_failure"
        return ExecutionResult(outcome=outcome, executor=self.name, is_mock=True,
                               solution_hash=sha256_text(f"mock-solution:{scenario.reference_hash}:{attempt_no}"),
                               diagnostics={"mock": True, "executed_code": False,
                                            "note": "scripted outcome from development fixture"})


def mock_embedding(text: str, dims: int = 64) -> list[float]:
    vec = [0.0] * dims
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(sha256_text(tok)[:12], 16)
        vec[h % dims] += 1.0 if (h >> 20) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [round(v / norm, 6) for v in vec]


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"

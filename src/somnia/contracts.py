"""Versioned public contracts.

These models are the boundary between Somnia's learner-side engine and other components
(the independent evaluator, the dashboard, CLI consumers). JSON Schemas are exported to
``contracts/v1`` with ``somnia contracts export``. Changing a field is a contract change and
needs a schema-version bump plus a DECISIONS.md entry.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = 1


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- experience
class EventReceipt(_Frozen):
    schema_version: Literal[1] = 1
    event_id: UUID
    session_id: UUID
    seq: int
    event_hash: str
    created: bool = Field(description="False when an identical idempotent retry returned the existing event")


class SnapshotInfo(_Frozen):
    schema_version: Literal[1] = 1
    snapshot_id: UUID
    project_id: UUID
    content_hash: str
    base_skill_set_id: UUID
    event_count: int
    created: bool


# ---------------------------------------------------------------- run configuration
class LimitsConfig(_Frozen):
    max_hypotheses: int = Field(4, ge=0)
    max_scenarios_per_hypothesis: int = Field(2, ge=0)
    max_executed_scenarios: int = Field(4, ge=0)
    max_attempts_per_scenario: int = Field(2, ge=1)
    max_candidates: int = Field(2, ge=0)


class GatewayConfig(_Frozen):
    """Upper-bound pricing used for reservations. In mock mode the unit is MOCK_MICRO (not money)."""

    provider: str
    model: str
    unit: Literal["CAD_MICRO", "MOCK_MICRO"]
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    input_micro_per_token: int = Field(ge=0)
    output_micro_per_token: int = Field(ge=0)


class GateConfig(_Frozen):
    validator: Literal["mock"] = "mock"
    allow_mock_gate_activation: bool = False


class ActivationConfig(_Frozen):
    auto_activate: bool = False


class RunConfig(_Frozen):
    schema_version: Literal[1] = 1
    mode: Literal["mock"] = "mock"
    mock_script: str
    gateway: GatewayConfig
    limits: LimitsConfig = LimitsConfig()
    gate: GateConfig = GateConfig()
    activation: ActivationConfig = ActivationConfig()
    embedding_spec: str = "mock-hash-v1"
    retrieval_max_skills: int = 3
    retrieval_token_ceiling: int = 1500


# ---------------------------------------------------------------- skills
class SkillBody(_Frozen):
    """The immutable, hashed content of a skill version."""

    slug: str
    title: str
    procedure: str
    applicability: dict = Field(description='e.g. {"domain": "python-reliability", "requires_tags": [...]}')
    exclusions: dict = Field(description='e.g. {"tags": ["irreversible-external-effect"]}')
    prerequisites: list[str] = []
    known_limits: str


class CandidateSkill(_Frozen):
    schema_version: Literal[1] = 1
    skill_version_id: UUID
    skill_id: UUID
    version: int
    parent_version_id: UUID | None
    content_hash: str
    token_length: int
    origin: Literal["mock", "model", "user"]
    body: SkillBody


class SkillSetMember(_Frozen):
    skill_slug: str
    skill_id: UUID
    skill_version_id: UUID
    version: int
    parent_version_id: UUID | None
    content_hash: str = Field(description="sha256(canonical_json({body, parent_version_id}))")
    gate_decision_id: UUID
    gate_is_mock: bool
    gate_independent: bool
    token_length: int
    body: SkillBody


class SkillSetExport(_Frozen):
    """Frozen skill-set view an evaluator consumes (v2). Verify it with
    ``somnia.export_verify.verify_skill_set_export(json, trusted_digest)``: the export digest is
    SHA-256 over the canonical JSON of the whole document and must come from a trusted channel,
    never from the document itself. See contracts/README.md."""

    schema_version: Literal[2] = 2
    canonicalization: Literal["somnia-cjson-v1"] = "somnia-cjson-v1"
    skill_set_id: UUID
    project_id: UUID
    version: int
    predecessor_id: UUID | None
    content_hash: str = Field(description="sha256(canonical_json({members: [{skill_id, skill_version_id, "
                                          "gate_decision_id}] sorted by skill_id}))")
    contains_mock_validation: bool = Field(description="True iff any member's gate_is_mock is true")
    members: list[SkillSetMember]

    @model_validator(mode="after")
    def _flags_consistent(self) -> SkillSetExport:
        if self.contains_mock_validation != any(m.gate_is_mock for m in self.members):
            raise ValueError("contains_mock_validation must equal any(member.gate_is_mock)")
        if any(m.gate_is_mock and m.gate_independent for m in self.members):
            raise ValueError("a mock-gated member cannot be independent")
        return self


# ---------------------------------------------------------------- gate
class PairedScore(_Frozen):
    task_id: str
    root_cluster_id: str
    repeat: int
    without_skill: float
    with_skill: float
    synthetic: bool = Field(description="True for fixture-scripted numbers that are not measurements")


class GateRequest(_Frozen):
    """What a validator receives. It never includes hidden oracles or sealed-task content.
    v2: ``baseline_skill_set`` is a SkillSetExport v2."""

    schema_version: Literal[2] = 2
    candidate: CandidateSkill
    baseline_skill_set: SkillSetExport
    suite_id: str


_MOCK_AND_INDEPENDENT = {"properties": {"is_mock": {"const": True}, "independent": {"const": True}},
                         "required": ["is_mock", "independent"]}


class GateVerdict(_Frozen):
    """What a validator returns. ``independent`` must be False for any mock or learner-side gate.
    A verdict with ``is_mock=true`` and ``independent=true`` is rejected by this model, by the
    exported JSON Schema (``not`` clause) and by the database CHECK constraint."""

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_extra={"not": _MOCK_AND_INDEPENDENT})

    schema_version: Literal[1] = 1
    candidate_hash: str
    decision: Literal["pass", "fail", "inconclusive"]
    reason: str
    validator: str
    validator_version: str
    suite_id: str
    suite_hash: str
    independent: bool
    is_mock: bool
    paired_scores: list[PairedScore] = []
    evidence_refs: list[str] = []

    @model_validator(mode="after")
    def _mock_is_never_independent(self) -> GateVerdict:
        if self.is_mock and self.independent:
            raise ValueError("a mock gate verdict cannot claim to be independent (is_mock and independent both true)")
        return self


# ---------------------------------------------------------------- activation
class ActivationResult(_Frozen):
    schema_version: Literal[1] = 1
    activation_id: UUID
    previous_set_id: UUID | None
    new_set_id: UUID
    new_set_version: int
    applied: bool = Field(description="False when this call was an idempotent replay of an earlier activation")


# ---------------------------------------------------------------- retrieval
class TaskScope(_Frozen):
    domain: str
    tags: list[str] = []


class RetrievalRequest(_Frozen):
    schema_version: Literal[1] = 1
    project_slug: str
    session_id: UUID
    task_query: str
    scope: TaskScope
    skill_set_id: UUID | None = Field(None, description="None means the project's active set at call time")
    token_ceiling: int = Field(1500, gt=0)
    max_skills: int = Field(3, ge=0)


class SelectedSkill(_Frozen):
    skill_slug: str
    skill_version_id: UUID
    version: int
    title: str
    procedure: str
    token_length: int
    distance: float
    gate_decision_id: UUID
    gate_is_mock: bool
    source_refs: list[str]


class ExcludedSkill(_Frozen):
    skill_slug: str
    skill_version_id: UUID
    reason: Literal["out_of_domain", "missing_required_tags", "excluded_by_scope", "max_skills", "token_ceiling"]
    detail: str


class RetrievalResult(_Frozen):
    schema_version: Literal[1] = 1
    retrieval_log_id: UUID
    skill_set_id: UUID
    skill_set_version: int
    skill_set_contains_mock_validation: bool
    embedding_spec: str
    selected: list[SelectedSkill]
    excluded: list[ExcludedSkill]
    token_count: int


# ---------------------------------------------------------------- execution (mock in this assignment)
class ScenarioSpec(_Frozen):
    """A rehearsal scenario. Reference/faulty implementations and the oracle are referenced by hash."""

    title: str
    intervention: str
    expected_invariant: str
    reference_hash: str
    faulty_hash: str
    oracle_hash: str


class ExecutionResult(_Frozen):
    schema_version: Literal[1] = 1
    outcome: Literal["fault_detected", "no_fault_detected", "task_failure", "invalid_scenario",
                     "agent_timeout", "infrastructure_failure", "safety_violation"]
    executor: str
    is_mock: bool
    solution_hash: str | None
    diagnostics: dict


EXPORTED_MODELS: dict[str, type[BaseModel]] = {
    "event_receipt": EventReceipt,
    "snapshot_info": SnapshotInfo,
    "run_config": RunConfig,
    "candidate_skill": CandidateSkill,
    "skill_set_export": SkillSetExport,
    "gate_request": GateRequest,
    "gate_verdict": GateVerdict,
    "activation_result": ActivationResult,
    "retrieval_request": RetrievalRequest,
    "retrieval_result": RetrievalResult,
    "scenario_spec": ScenarioSpec,
    "execution_result": ExecutionResult,
}

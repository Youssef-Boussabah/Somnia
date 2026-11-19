"""Committed JSON Schemas must match the pydantic contracts (regenerate with `somnia contracts export`)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from conftest import ROOT
from somnia.contracts import EXPORTED_MODELS


def test_committed_schemas_match_models():
    for name, model in EXPORTED_MODELS.items():
        committed = json.loads((ROOT / "contracts" / "v1" / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert committed == model.model_json_schema(), f"{name}: schema drift; run `somnia contracts export`"


def test_mock_verdict_cannot_be_mistaken_for_independent():
    schema = EXPORTED_MODELS["gate_verdict"].model_json_schema()
    assert {"independent", "is_mock"} <= set(schema["required"])


def _verdict(**flags):
    return {"schema_version": 1, "candidate_hash": "a" * 64, "decision": "pass", "reason": "development example",
            "validator": "example", "validator_version": "0", "suite_id": "s", "suite_hash": "h", **flags}


VALID_MOCK = _verdict(is_mock=True, independent=False)
VALID_INDEPENDENT = _verdict(is_mock=False, independent=True)
CONTRADICTORY = _verdict(is_mock=True, independent=True)


def test_gate_verdict_model_rejects_mock_claiming_independence():
    from somnia.contracts import GateVerdict

    assert GateVerdict.model_validate(VALID_MOCK).is_mock
    assert GateVerdict.model_validate(VALID_INDEPENDENT).independent
    with pytest.raises(ValidationError, match="independent"):
        GateVerdict.model_validate(CONTRADICTORY)
    with pytest.raises(ValidationError, match="independent"):
        GateVerdict.model_validate_json(json.dumps(CONTRADICTORY))


def test_gate_verdict_json_schema_rejects_mock_claiming_independence():
    committed = json.loads((ROOT / "contracts" / "v1" / "gate_verdict.schema.json").read_text(encoding="utf-8"))
    for schema in (committed, EXPORTED_MODELS["gate_verdict"].model_json_schema()):
        Draft202012Validator.check_schema(schema)
        v = Draft202012Validator(schema)
        assert v.is_valid(VALID_MOCK) and v.is_valid(VALID_INDEPENDENT)
        assert not v.is_valid(CONTRADICTORY)

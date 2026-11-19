"""Committed JSON Schemas must match the pydantic contracts (regenerate with `somnia contracts export`)."""

from __future__ import annotations

import json

from conftest import ROOT
from somnia.contracts import EXPORTED_MODELS


def test_committed_schemas_match_models():
    for name, model in EXPORTED_MODELS.items():
        committed = json.loads((ROOT / "contracts" / "v1" / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert committed == model.model_json_schema(), f"{name}: schema drift; run `somnia contracts export`"


def test_mock_verdict_cannot_be_mistaken_for_independent():
    schema = EXPORTED_MODELS["gate_verdict"].model_json_schema()
    assert {"independent", "is_mock"} <= set(schema["required"])

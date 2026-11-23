"""Checks of the Stage input slot registry and the rules derived from it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast, get_args

import pytest
from pydantic import ValidationError

from sdi_pipeline_integration._stage_registry import (
    EXPECTED_STAGE_INPUT_SLOTS,
    SLOT_BY_NAME,
    SLOT_BY_SCHEMA,
    SLOTS,
)
from sdi_pipeline_integration._stage_runtime import OUTPUT_MODELS, SLOT_MODELS

CASES_ROOT = Path(__file__).parents[1] / "fixtures" / "cases"
DOMAIN_PAYLOADS = (
    "composition-s-04-tc-03-c-05/composition-blueprint.json",
    "composition-s-04-tc-03-c-05/deployment-schema.json",
    "image-build-s-04-tc-03-c-05/image-build-result.json",
    "cv-s-04-tc-03-c-05/validation-evidence.json",
    "cd-s-04-tc-03-c-05/deployment-result.json",
)


def test_slot_names_and_schema_versions_are_unique() -> None:
    assert len(SLOT_BY_NAME) == len(SLOT_BY_SCHEMA) == len(SLOTS)


def test_each_stage_input_is_committed_supplied_or_an_earlier_output() -> None:
    stages = list(EXPECTED_STAGE_INPUT_SLOTS)
    for index, (stage, names) in enumerate(EXPECTED_STAGE_INPUT_SLOTS.items()):
        for name in names:
            origin = SLOT_BY_NAME[name].origin
            assert origin in {"committed", "supplied", *stages[:index]}, (stage, name)


def test_each_stage_grants_its_inputs_in_canonical_order() -> None:
    canonical = [slot.name for slot in SLOTS]
    for names in EXPECTED_STAGE_INPUT_SLOTS.values():
        assert list(names) == sorted(names, key=canonical.index)


def test_each_slot_has_one_parse_model_of_its_schema_version() -> None:
    assert list(SLOT_MODELS) == [slot.name for slot in SLOTS]
    for name, model in SLOT_MODELS.items():
        annotation = model.model_fields["schema_version"].annotation
        assert get_args(annotation) == (SLOT_BY_NAME[name].schema_version,)


def _payload(relative: str) -> dict[str, object]:
    return json.loads((CASES_ROOT / relative).read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", DOMAIN_PAYLOADS)
def test_domain_output_names_each_producer_input_in_order(relative: str) -> None:
    payload = _payload(relative)
    model = OUTPUT_MODELS[str(payload["schema_version"])]
    model.model_validate(payload, strict=True)
    sources = cast("list[dict[str, str]]", payload["source_inputs"])
    ungranted = [
        {**sources[0], "slot": "deployment_result"},
        *sources[1:],
    ]
    for variant in (ungranted, sources[1:], [*sources[1:], sources[0]]):
        with pytest.raises(ValidationError, match="input slot once, in order"):
            model.model_validate(payload | {"source_inputs": variant}, strict=True)

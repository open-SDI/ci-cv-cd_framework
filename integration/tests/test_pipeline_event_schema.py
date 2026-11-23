"""The pipeline-event row model and its generated schema."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import ValidationError

from sdi_pipeline_integration._domain_contracts import (
    FAILED_IMAGE_REASONS,
    K8S_REASONS,
)
from sdi_pipeline_integration._pipeline_event_catalogue import STEP_CODES
from sdi_pipeline_integration._pipeline_events import (
    PipelineEventRow,
    render_prism_line,
)
from sdi_pipeline_integration._schemas import check_schemas
from tests._pipeline_events import (
    SCHEMA_DIRECTORY,
    SCHEMA_PATH,
    row_fields,
    worst_case_fields,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

type Schema = dict[str, JsonValue]


def _schema() -> Schema:
    return json.loads(SCHEMA_PATH.read_bytes())


def _validate(document: dict[str, JsonValue]) -> None:
    Draft202012Validator(_schema()).validate(document)  # pyright: ignore[reportUnknownMemberType]


def _wire(fields: dict[str, JsonValue]) -> dict[str, JsonValue]:
    line = render_prism_line(PipelineEventRow.model_validate(fields))
    return json.loads(line.removeprefix("PRISM_EVENT "))


def _objects(schema: Schema) -> list[Schema]:
    definitions = cast("dict[str, Schema]", schema.get("$defs", {}))
    return [schema, *(d for d in definitions.values() if d.get("type") == "object")]


def _property(name: str) -> Schema:
    properties = cast("dict[str, Schema]", _schema()["properties"])
    return properties[name]


def test_committed_schema_is_fresh() -> None:
    check_schemas(SCHEMA_DIRECTORY)


def test_model_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        PipelineEventRow.model_validate(row_fields(attempt=2))


@pytest.mark.parametrize("name", list(PipelineEventRow.model_fields))
def test_model_requires_every_key(name: str) -> None:
    fields = row_fields()
    del fields[name]
    with pytest.raises(ValidationError, match=name):
        PipelineEventRow.model_validate(fields)


def test_schema_allows_unknown_properties_on_every_object() -> None:
    objects = _objects(_schema())
    assert len(objects) > 1, "the item models are missing from $defs"
    for definition in objects:
        assert definition["additionalProperties"] is True, definition.get("title")
    _validate({**_wire(worst_case_fields()), "attempt": 2})


def test_schema_requires_every_property() -> None:
    assert _schema()["required"] == list(PipelineEventRow.model_fields)


def test_rows_validate_against_the_committed_schema() -> None:
    _validate(_wire(row_fields()))
    _validate(_wire(worst_case_fields()))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("cv_status", "DONE"),
        ("stage", "ci"),
        ("target", "sdv"),
        ("architecture", "riscv64"),
        ("platform_status", "match"),
        ("schema_version", "sdi.pipeline-event/v2"),
    ],
)
def test_schema_rejects_a_value_outside_a_closed_enum(
    name: str, value: JsonValue
) -> None:
    with pytest.raises(SchemaValidationError):
        _validate({**_wire(row_fields()), name: value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("step", "warming-up"),
        ("mission_setup", "skipped"),
        ("failure_reason", "sdi.cv.some-new-code"),
    ],
)
def test_schema_and_model_accept_a_new_open_code(name: str, value: JsonValue) -> None:
    fields = row_fields(**{name: value})
    _validate(_wire(fields))
    assert getattr(PipelineEventRow.model_validate(fields), name) == value


def test_open_codes_list_their_examples() -> None:
    assert _property("step")["examples"] == list(STEP_CODES)
    definitions = cast("dict[str, Schema]", _schema()["$defs"])
    failure = cast("dict[str, Schema]", definitions["ImageFailure"]["properties"])
    assert failure["reason"]["examples"] == list(FAILED_IMAGE_REASONS)
    placement = cast("dict[str, Schema]", definitions["PlacementIssue"]["properties"])
    examples = cast("list[str]", placement["reason"]["examples"])
    assert set(K8S_REASONS) <= set(examples)
    assert "blocked" not in examples


def test_pipeline_events_doc_names_every_field_and_step() -> None:
    doc = (SCHEMA_DIRECTORY.parents[1] / "docs" / "pipeline-events.md").read_text()
    missing = [
        name
        for name in (*PipelineEventRow.model_fields, *STEP_CODES)
        if f"`{name}`" not in doc
    ]
    assert missing == [], f"docs/pipeline-events.md does not name {missing}"

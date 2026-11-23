"""Valid pipeline-event rows, and golden projections turned into console bytes."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

from sdi_pipeline_integration._cv_result import REQUIREMENTS, SCRIPTED
from sdi_pipeline_integration._pipeline_event_catalogue import MESSAGE_MAX_BYTES
from sdi_pipeline_integration._pipeline_event_check import load_golden
from sdi_pipeline_integration._pipeline_events import PipelineEventRow, write_event

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic import JsonValue

    from sdi_pipeline_integration._pipeline_event_check import Golden

SCHEMA_DIRECTORY: Final = Path(__file__).parents[1] / "schemas"
SCHEMA_PATH: Final = SCHEMA_DIRECTORY / "pipeline-event-v1.schema.json"

EXECUTION_ID: Final = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
IDENTITY: Final[dict[str, JsonValue]] = {
    "execution_id": EXECUTION_ID,
    "composition_id": "C-05",
    "scenario_id": "S-04",
    "testcase_id": "TC-03",
    "profile_id": "waffle-jetson-arm64",
    "target": "SDV",
    "architecture": "arm64",
}
FIRST_EMITTED_AT: Final = "2026-10-28T09:00:00.000Z"

SAMPLES: Final[dict[str, JsonValue]] = {
    "ci_duration_ms": 845_000,
    "blueprint_ref": "stages/composition/composition-blueprint.json",
    "deployment_schema_ref": "stages/composition/deployment-schema.json",
    "image_build_result_ref": "stages/image-build/image-build-result.json",
    "image_ref": [
        f"registry:5000/sdi/face-recognition:face-recognition-arm64-{EXECUTION_ID}",
        f"registry:5000/sdi/navigation:navigation-arm64-{EXECUTION_ID}",
    ],
    "image_digest": ["sha256:" + "0" * 64, "sha256:" + "1" * 64],
    "images_built": 2,
    "images_total": 2,
    "image_failures": [],
    "platform_status": "MATCH",
    "detected_platform": "linux/arm64",
    "cv_duration_ms": 1_212_000,
    "validation_evidence_ref": "stages/cv/validation-evidence.json",
    "cv_platform": "linux/amd64",
    "cv_services_participating": 2,
    "cv_services_blocked": 0,
    "cv_services_total": 2,
    "cv_service_issues": [],
    "mission_setup": "passed",
    "mission_steps": [
        {"id": step, "status": "scripted" if step in SCRIPTED else "passed"}
        for step in REQUIREMENTS
    ],
    "cd_duration_ms": 301_000,
    "deployment_result_ref": "stages/cd/deployment-result.json",
    "cd_placements_deployed": 2,
    "cd_placements_blocked": 0,
    "cd_placements_total": 2,
    "placement_issues": [],
}


def row_fields(**changes: JsonValue) -> dict[str, JsonValue]:
    """Return a `run-accepted` row's fields, every result null, then `changes`."""
    fields: dict[str, JsonValue] = dict.fromkeys(PipelineEventRow.model_fields)
    fields.update(
        {
            "schema_version": "sdi.pipeline-event/v1",
            "emitted_at": FIRST_EMITTED_AT,
            "final": False,
            "stage": None,
            "step": "run-accepted",
            "message": "Run accepted for C-05 on waffle-jetson-arm64",
            **IDENTITY,
            "composition_status": "PENDING",
            "image_build_status": "PENDING",
            "ci_status": "PENDING",
            "cv_status": "PENDING",
            "cd_status": "PENDING",
        }
    )
    fields.update(changes)
    return fields


def row(**changes: JsonValue) -> PipelineEventRow:
    """Return `row_fields(**changes)`, validated."""
    return PipelineEventRow.model_validate(row_fields(**changes))


def worst_case_fields() -> dict[str, JsonValue]:
    """Return a CD `stage-finished` row with every field set, at its longest."""
    longest_slug = "a" * 63
    fields = row_fields(
        stage="cd",
        step="stage-finished",
        message="x" * MESSAGE_MAX_BYTES,
        composition_status="NOT_EVALUATED",
        image_build_status="NOT_EVALUATED",
        ci_status="NOT_EVALUATED",
        cv_status="NOT_EVALUATED",
        cd_status="NOT_EVALUATED",
        failure_reason=f"sdi.{'a' * 61}.{'b' * 61}",
        **SAMPLES,
    )
    fields["image_failures"] = [
        {"service_id": longest_slug, "reason": "unresolved-placement"}
    ]
    fields["cv_service_issues"] = [
        {
            "service_id": longest_slug,
            "participation": "did_not_participate",
            "reason": "container-exited",
        }
    ]
    fields["placement_issues"] = [
        {
            "service_id": longest_slug,
            "host": longest_slug,
            "status": "not_evaluated",
            "reason": "no-image-for-architecture",
        }
    ]
    return fields


GOLDEN_DIRECTORY: Final = Path(__file__).parent / "data" / "pipeline-events"
SHELL_TRACE: Final = b"+ uv run --frozen sdi-integration execute-jenkins-stage cv\n"
RESULT_FIELDS: Final = tuple(PipelineEventRow.model_fields)[
    tuple(PipelineEventRow.model_fields).index("ci_duration_ms") :
]


def golden_names() -> list[str]:
    """Return every golden file's name, sorted."""
    return sorted(path.stem for path in GOLDEN_DIRECTORY.glob("*.txt"))


def golden(name: str) -> Golden:
    """Load one golden with the production parser."""
    return load_golden(GOLDEN_DIRECTORY / f"{name}.txt")


def written(event: PipelineEventRow) -> bytes:
    """Return the bytes `write_event` writes for one row."""
    with tempfile.TemporaryFile() as sink:
        write_event(event, fd=sink.fileno())
        sink.seek(0)
        return sink.read()


def console(
    rows: Sequence[PipelineEventRow],
    *,
    noise: bool = True,
    decorate: Callable[[bytes], bytes] | None = None,
) -> bytes:
    """Return rows as a Jenkins console shows them, written by `write_event`."""
    parts: list[bytes] = []
    for event in rows:
        if noise:
            parts.append(SHELL_TRACE)
        pair = written(event)
        if decorate is not None:
            pair = b"".join(map(decorate, pair.splitlines(keepends=True)))
        parts.append(pair)
    return b"".join(parts)


def with_row(
    rows: Sequence[PipelineEventRow], index: int, **changes: JsonValue
) -> list[PipelineEventRow]:
    """Return a copy of `rows` with one row changed and validated again."""
    changed = list(rows)
    changed[index] = PipelineEventRow.model_validate(
        {**rows[index].model_dump(mode="json"), **changes}
    )
    return changed

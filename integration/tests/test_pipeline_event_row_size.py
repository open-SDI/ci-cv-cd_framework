"""A G1 final line with every repository service, and the committed example lines."""

from __future__ import annotations

import json

import pytest

from sdi_pipeline_integration._pipeline_event_check import check_events
from sdi_pipeline_integration._pipeline_event_rows import EventLine
from sdi_pipeline_integration._pipeline_events import PRISM_PREFIX, PipelineEventRow
from sdi_pipeline_integration._service_descriptions import read_service_repository
from tests._pipeline_event_examples import EXAMPLE_DIRECTORY, EXAMPLES
from tests._pipeline_event_scenarios import golden_rows
from tests._pipeline_event_world import (
    SERVICE_REPOSITORY,
    Placement,
    build,
    cd_deployed,
    composition_ok,
    cv_passed,
    images_built,
)
from tests._pipeline_events import console, golden, written
from tests._stage_attempts import finalized

REGENERATE = "uv run --frozen python -m tests._pipeline_event_examples"
EXAMPLE_NULLS = {
    "mid-cv": {
        "failure_reason",
        "image_archive",
        "cv_duration_ms",
        "validation_evidence_ref",
        "cv_platform",
        "cv_services_participating",
        "cv_services_blocked",
        "cv_services_total",
        "cv_service_issues",
        "cd_duration_ms",
        "deployment_result_ref",
        "cd_placements_deployed",
        "cd_placements_blocked",
        "cd_placements_total",
        "placement_issues",
    },
    "g1-final": {"stage", "failure_reason", "image_archive"},
}

MISSION_STEPS = 50


def _repository_placements() -> tuple[Placement, ...]:
    return tuple(
        Placement(
            item.description.service_id,
            min(item.description.artifacts or {}),
            ("waffle", "orin")[index % 2],
        )
        for index, item in enumerate(read_service_repository(SERVICE_REPOSITORY))
    )


def _g1_final_with_every_service() -> PipelineEventRow:
    placements = _repository_placements()
    held = [
        composition_ok(placements),
        images_built(placements),
        cv_passed(placements),
        cd_deployed(placements),
    ]
    final = build(
        finalized(held, run_deadline_expired=False),
        line=EventLine(None, "run-finished"),
    )
    steps = [
        {"id": f"step-{index:02d}", "status": "passed"}
        for index in range(MISSION_STEPS)
    ]
    return PipelineEventRow.model_validate(
        {**final.model_dump(mode="json"), "mission_steps": steps}
    )


def test_a_g1_final_line_with_every_service_is_written_whole() -> None:
    row = _g1_final_with_every_service()
    services = [
        path for path in SERVICE_REPOSITORY.iterdir() if (path / "SDI.md").is_file()
    ]
    assert row.image_ref is not None
    assert len(row.image_ref) == len(services)

    data = written(row)

    sdi_line, prism_line = data.splitlines()
    assert sdi_line.startswith(b"[sdi] ")
    assert len(prism_line) > 4096
    assert (
        PipelineEventRow.model_validate_json(
            prism_line.removeprefix(PRISM_PREFIX.encode())
        )
        == row
    )
    rows = [*golden_rows("g1")[:-1], row]
    assert check_events(console(rows), None) == []
    assert check_events(console(rows), golden("g1")) == []


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_lines_are_current(name: str) -> None:
    row = EXAMPLES[name]()
    committed = (EXAMPLE_DIRECTORY / f"{name}.txt").read_bytes()

    assert committed == written(row), f"{name}.txt is stale; run `{REGENERATE}`"
    sdi_line, prism_line = committed.splitlines()
    assert sdi_line.startswith(b"[sdi] ")
    fields = json.loads(prism_line.removeprefix(PRISM_PREFIX.encode()))
    assert {key for key, value in fields.items() if value is None} == EXAMPLE_NULLS[
        name
    ]

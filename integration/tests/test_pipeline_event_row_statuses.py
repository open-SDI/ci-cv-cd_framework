"""A pipeline-event row's statuses, `ci_status`, `failure_reason` and line head."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError

from sdi_pipeline_integration._pipeline_event_check import status_vector
from sdi_pipeline_integration._pipeline_event_rows import EventLine, StageFinished
from sdi_pipeline_integration._pipeline_events import render_sdi_line
from sdi_pipeline_integration._stage_contracts import AcceptedAttemptEnvelope
from sdi_pipeline_integration._stage_registry import EXPECTED_STAGE_INPUT_SLOTS
from sdi_pipeline_integration._stage_runtime import StageExecution
from tests._pipeline_event_world import (
    adapter_failed,
    attempt,
    blueprint,
    build,
    cd_deployed,
    composition_ok,
    cv_passed,
    deployment_schema,
    domain_failed,
    fixture_composition,
    image_build_result,
    images_built,
)
from tests._pipeline_events import EXECUTION_ID
from tests._stage_attempts import STARTED_AT, finalized, skipped

if TYPE_CHECKING:
    from sdi_pipeline_integration._pipeline_event_catalogue import PipelineStatus
    from sdi_pipeline_integration._pipeline_events import (
        EventScope,
        PipelineEventRow,
    )

DEADLINE = "sdi.run.deadline-exceeded"
LETTERS = {
    "PENDING": "P",
    "RUNNING": "R",
    "SUCCEEDED": "S",
    "FAILED": "F",
    "SKIPPED": "K",
    "NOT_EVALUATED": "N",
}
PROCESSES: dict[str, dict[str, object] | None] = {
    "none": None,
    "exit-0": {"termination": "exited", "exit_code": 0},
    "exit-1": {"termination": "exited", "exit_code": 1},
    "signaled": {"termination": "signaled", "signal": 9},
    "timed-out": {"termination": "timed_out"},
    "lost": {"termination": "lost"},
}
REASONS: dict[str, tuple[str, str] | None] = {
    "none": None,
    "deadline": ("dependency", DEADLINE),
    "dependency": ("dependency", "sdi.dependency.fixture-evidence"),
    "domain": ("domain", "sdi.cv.failed"),
    "adapter": ("adapter", "sdi.adapter.nonzero-exit"),
    "fixture": ("fixture", "sdi.fixture.cv-executed"),
}


def _letters(row: PipelineEventRow) -> str:
    return " ".join(LETTERS[status] for status in status_vector(row))


def _envelope(shape: tuple[str, ...]) -> AcceptedAttemptEnvelope | None:
    lifecycle, conclusion, mode, domain, reason, process, accepted = shape
    facts = PROCESSES[process]
    cause = REASONS[reason]
    data: dict[str, Any] = {
        "schema_version": "sdi.accepted-attempt-envelope/v1",
        "correlation": {
            "execution_id": EXECUTION_ID,
            "stage": "cv",
            "attempt_number": 1,
        },
        "lifecycle_state": lifecycle,
        "execution_conclusion": conclusion,
        "implementation_mode": mode,
        "domain_outcome": domain,
        "adapter_response_accepted": accepted == "accepted",
        "accepted_inputs": []
        if lifecycle == "skipped"
        else [
            {
                "slot": slot,
                "source_path": f"inputs/{slot}",
                "schema_version": "sdi.test/v1",
                "byte_size": 0,
                "sha256": "0" * 64,
            }
            for slot in EXPECTED_STAGE_INPUT_SLOTS["cv"]
        ],
        "accepted_files": [],
    }
    if facts is not None:
        data["process"] = {
            "started_at": STARTED_AT,
            "finished_at": STARTED_AT,
            "duration_ms": 0,
            **facts,
        }
    if cause is not None:
        data["reason"] = {"category": cause[0], "code": cause[1], "summary": "A test."}
    try:
        return AcceptedAttemptEnvelope.model_validate(data, strict=True, extra="forbid")
    except ValidationError:
        return None


def _spec_5_1(envelope: AcceptedAttemptEnvelope) -> PipelineStatus:
    """Spec 5.1's table, row by row."""
    code = None if envelope.reason is None else envelope.reason.code
    if envelope.execution_conclusion == "skipped":
        return "FAILED" if code == DEADLINE else "SKIPPED"
    if envelope.execution_conclusion in {"failed", "timed_out"}:
        return "FAILED"
    by_outcome: dict[str, PipelineStatus] = {
        "succeeded": "SUCCEEDED",
        "failed": "FAILED",
        "not_evaluated": "NOT_EVALUATED",
    }
    return by_outcome[envelope.domain_outcome]


def _accepted_shapes() -> list[AcceptedAttemptEnvelope]:
    shapes = itertools.product(
        ("completed", "skipped"),
        ("succeeded", "failed", "timed_out", "skipped"),
        ("implemented", "fixture", "not_implemented"),
        ("succeeded", "failed", "not_evaluated"),
        tuple(REASONS),
        tuple(PROCESSES),
        ("accepted", "unaccepted"),
    )
    return [envelope for shape in shapes if (envelope := _envelope(shape)) is not None]


def test_every_envelope_the_validators_accept_gets_its_spec_5_1_status() -> None:
    shapes = _accepted_shapes()
    held = [composition_ok(), images_built()]
    seen: set[PipelineStatus] = set()
    for envelope in shapes:
        execution = StageExecution(envelope=envelope, files={}, outputs={})
        row = build(held, line=StageFinished(execution))
        assert row.cv_status == _spec_5_1(envelope), envelope
        seen.add(row.cv_status)
        if row.cv_status == "SUCCEEDED":
            assert envelope.implementation_mode == "implemented", envelope

    assert seen == {"SUCCEEDED", "FAILED", "NOT_EVALUATED", "SKIPPED"}


def test_a_stage_is_running_on_its_own_lines_and_pending_before() -> None:
    row = build([composition_ok()], line=EventLine("image_build", "stage-started"))

    assert _letters(row) == "S R P P"


def test_an_attempt_for_the_running_stage_does_not_end_it() -> None:
    held = [composition_ok(), images_built()]

    row = build(held, line=EventLine("image_build", "publishing"))

    assert _letters(row) == "S R P P"


def test_executions_match_stages_by_correlation_not_position() -> None:
    held = [cv_passed(), images_built(), composition_ok()]

    row = build(held, line=EventLine("cd", "stage-started"))

    assert _letters(row) == "S S S R"


def test_the_last_execution_for_a_stage_wins() -> None:
    failed = domain_failed("cv", "sdi.cv.failed")
    held = [composition_ok(), images_built()]

    assert (
        _letters(
            build([*held, failed, cv_passed()], line=EventLine("cd", "stage-started"))
        )
        == "S S S R"
    )
    assert (
        _letters(
            build([*held, cv_passed(), failed], line=EventLine(None, "run-accepted"))
        )
        == "S S F K"
    )


def test_a_stage_finished_head_replaces_the_held_attempt_of_its_stage() -> None:
    held = [composition_ok(), images_built(), cv_passed()]

    row = build(held, line=StageFinished(domain_failed("cv", "sdi.cv.failed")))

    assert _letters(row) == "S S F K"
    assert row.failure_reason == "sdi.cv.failed"


def test_a_blocking_attempt_skips_later_stages_provisionally() -> None:
    row = build(
        [composition_ok()],
        line=StageFinished(
            domain_failed("image_build", "sdi.image-build.builder-unreachable")
        ),
    )

    assert _letters(row) == "S F K K"
    assert row.ci_status == "FAILED"


def test_a_blocking_attempt_skips_later_stages_in_a_single_stage_row_too() -> None:
    failed = domain_failed("image_build", "sdi.image-build.builder-unreachable")

    row = build(line=StageFinished(failed), scope="single_stage")

    assert _letters(row) == "P F K K"
    assert row.ci_status == "FAILED"


def test_a_not_evaluated_implemented_attempt_does_not_block() -> None:
    not_evaluated = attempt(
        "cv", domain="not_evaluated", reason=("domain", "sdi.cv.not-evaluated")
    )
    held = [composition_ok(), images_built()]

    row = build(held, line=StageFinished(not_evaluated))

    assert _letters(row) == "S S N P"
    assert row.failure_reason == "sdi.cv.not-evaluated"


@pytest.mark.parametrize(
    ("scope", "expected"), [("run", "RUNNING"), ("single_stage", "SUCCEEDED")]
)
def test_ci_status_with_one_terminal_half_depends_on_scope(
    scope: EventScope, expected: str
) -> None:
    row = build(line=StageFinished(images_built()), scope=scope)

    assert (row.composition_status, row.image_build_status) == ("PENDING", "SUCCEEDED")
    assert row.ci_status == expected


def test_ci_status_is_the_worse_terminal_half() -> None:
    fixture_skip = skipped("image_build", "sdi.dependency.implemented-evidence")

    row = build([composition_ok()], line=StageFinished(fixture_skip))

    assert row.ci_status == "SKIPPED"


def test_failure_reason_is_null_while_every_stage_is_succeeded_pending_or_running() -> (
    None
):
    held = [composition_ok(), images_built()]

    assert (
        build(
            held, line=EventLine("cv", "adapter-running", {"limit_min": 45})
        ).failure_reason
        is None
    )
    assert (
        build(
            [*held, cv_passed(), cd_deployed()], line=EventLine(None, "run-finished")
        ).failure_reason
        is None
    )


def test_a_failed_stage_outranks_an_earlier_not_evaluated_one() -> None:
    not_evaluated = attempt(
        "cv", domain="not_evaluated", reason=("domain", "sdi.cv.not-evaluated")
    )
    held = [composition_ok(), images_built(), not_evaluated]

    row = build(held, line=StageFinished(domain_failed("cd", "sdi.cd.failed")))

    assert row.failure_reason == "sdi.cd.failed"


def test_a_red_skip_outranks_an_earlier_not_evaluated_stage() -> None:
    row = build(
        [fixture_composition()],
        line=StageFinished(skipped("image_build", "sdi.dependency.fixture-evidence")),
    )

    assert row.failure_reason == "sdi.dependency.fixture-evidence"


def test_within_a_tier_the_earliest_stage_wins() -> None:
    first = attempt(
        "composition",
        domain="not_evaluated",
        reason=("domain", "sdi.composition.a"),
        documents={
            "composition_blueprint": blueprint(),
            "deployment_schema": deployment_schema(),
        },
    )
    second = attempt(
        "image_build",
        domain="not_evaluated",
        reason=("domain", "sdi.image-build.b"),
        documents={"image_build_result": image_build_result()},
    )

    row = build([first], line=StageFinished(second))

    assert row.failure_reason == "sdi.composition.a"
    assert row.message == "Image build finished: NOT_EVALUATED (sdi.image-build.b)"


def test_prerequisite_blocked_is_never_the_failure_reason() -> None:
    held = finalized(
        [
            composition_ok(),
            skipped("image_build", "sdi.dependency.implemented-evidence"),
        ],
        run_deadline_expired=False,
    )

    row = build(held, line=EventLine(None, "run-finished"))

    assert _letters(row) == "S K K K"
    assert row.failure_reason == "sdi.dependency.implemented-evidence"


def test_a_not_evaluated_attempt_without_a_reason_gives_none() -> None:
    held = [composition_ok(), images_built()]

    row = build(held, line=StageFinished(attempt("cv", domain="not_evaluated")))

    assert row.cv_status == "NOT_EVALUATED"
    assert row.failure_reason is None
    assert row.message == "CV finished: NOT_EVALUATED"


def test_a_run_deadline_skip_is_failed_on_the_final_line() -> None:
    held = finalized([composition_ok(), images_built()], run_deadline_expired=True)

    row = build(held, line=EventLine(None, "run-finished"))

    assert _letters(row) == "S S F K"
    assert row.failure_reason == DEADLINE
    assert row.message == "Final: CI SUCCEEDED, CV FAILED, CD SKIPPED"
    assert row.final


@pytest.mark.parametrize(
    ("head", "message"),
    [
        (
            EventLine(None, "run-accepted"),
            "Run accepted for C-05 on waffle-jetson-arm64",
        ),
        (
            EventLine("cv", "adapter-running", {"limit_min": 30}),
            "CV work started (limit 30 min)",
        ),
        (
            EventLine("cv", "mission", {"m": 3}, heartbeat=True),
            "Mission still running, 3 min",
        ),
        (
            EventLine(
                "cv",
                "mission-step",
                {"mission_step": "transfer-book", "i": 6, "n": 8},
            ),
            "Step transfer-book: scripted (6/8)",
        ),
    ],
)
def test_the_message_renders_the_heads_step(head: EventLine, message: str) -> None:
    row = build([composition_ok(), images_built()], line=head)

    assert row.message == message
    assert not row.final


def test_a_timed_out_stage_finishes_with_its_own_reason() -> None:
    timed_out = adapter_failed(
        "cv",
        "sdi.stage.deadline-exceeded",
        conclusion="timed_out",
        termination="timed_out",
    )

    row = build([composition_ok(), images_built()], line=StageFinished(timed_out))

    assert row.message == "CV finished: FAILED (sdi.stage.deadline-exceeded)"
    assert render_sdi_line(row) == (
        "[sdi] C-05 CV > stage-finished  FAILED  "
        "CV finished: FAILED (sdi.stage.deadline-exceeded)"
    )


@pytest.mark.parametrize(
    ("stage", "step", "slots", "heartbeat"),
    [
        (None, "stage-started", {}, False),
        ("cv", "run-accepted", {}, False),
        ("cv", "stage-finished", {"status": "SUCCEEDED"}, False),
        ("cd", "pull", {"service_id": "nav2-navigation", "i": 1, "n": 3}, False),
        (None, "run-accepted", {"composition_id": "C-05", "profile_id": "x"}, False),
        ("cv", "adapter-running", {}, False),
        ("cv", "adapter-running", {"limit_min": -1}, False),
        ("cv", "adapter-running", {"limit_min": True}, False),
        ("cv", "adapter-running", {"limit_min": "30"}, False),
        ("cv", "pull", {"service_id": "Nav2", "i": 1, "n": 3}, False),
        (
            "cv",
            "pull-blocked",
            {"service_id": "nav2", "pull_reason": "gone", "i": 1, "n": 3},
            False,
        ),
        ("cv", "stage-started", {"Stage": "CV"}, False),
        ("cv", "stage-started", {}, True),
        ("cv", "warming-up", {}, False),
        ("validation", "stage-started", {}, False),
    ],
)
def test_an_event_line_that_breaks_the_catalogue_is_refused(
    stage: str | None, step: str, slots: dict[str, object], *, heartbeat: bool
) -> None:
    with pytest.raises(ValueError, match="line: "):
        EventLine(
            cast("Any", stage),
            cast("Any", step),
            cast("Any", slots),
            heartbeat=heartbeat,
        )

"""How each golden's Stages ended, as accepted attempts, and the rows they build."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, cast

from sdi_pipeline_integration._pipeline_event_rows import EventLine, StageFinished
from sdi_pipeline_integration._stage_registry import STAGES
from tests._pipeline_event_world import (
    EMITTED_AT,
    PLACEMENTS,
    adapter_failed,
    build,
    cd_deployed,
    composition_ok,
    cv_mission_failed,
    cv_not_evaluated,
    cv_passed,
    cv_service_absent,
    deployment_result,
    domain_failed,
    fixture_composition,
    fixture_images,
    images_built,
)
from tests._pipeline_events import console, golden
from tests._stage_attempts import finalized, skipped

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sdi_pipeline_integration._pipeline_event_catalogue import StepCode
    from sdi_pipeline_integration._pipeline_event_rows import LineHead
    from sdi_pipeline_integration._pipeline_events import PipelineEventRow
    from sdi_pipeline_integration._stage_registry import StageName
    from sdi_pipeline_integration._stage_runtime import StageExecution

type Attempt = Callable[[], StageExecution]


@dataclass(frozen=True)
class Scenario:
    """How one golden's Stages ended.

    `finished` gives the attempt each `stage-finished` line publishes. `lost`
    Stages printed `stage-finished` but never reached Finalize (G9's race).
    `limit_min` overrides every `{limit_min}`, which is otherwise the Stage's
    grant work limit.
    """

    finished: Mapping[StageName, Attempt]
    lost: frozenset[StageName] = frozenset()
    run_deadline_expired: bool = False
    limit_min: int | None = None


OK: Final[dict[StageName, Attempt]] = {
    "composition": composition_ok,
    "image_build": images_built,
    "cv": cv_passed,
    "cd": cd_deployed,
}


def _ok(**changes: Attempt) -> dict[StageName, Attempt]:
    return {**OK, **cast("dict[StageName, Attempt]", changes)}


def _cd_failed(reason: str) -> Attempt:
    def failed() -> StageExecution:
        result = deployment_result(failed={PLACEMENTS[1].service_id: reason})
        return domain_failed("cd", "sdi.cd.failed", {"deployment_result": result})

    return failed


SCENARIOS: Final[dict[str, Scenario]] = {
    "g1": Scenario(OK),
    "g2": Scenario(_ok(cv=cv_mission_failed)),
    "g2b": Scenario(_ok(cv=cv_service_absent)),
    "g3": Scenario(_ok(cd=_cd_failed("CrashLoopBackOff"))),
    "g4": Scenario(
        _ok(
            image_build=lambda: domain_failed(
                "image_build", "sdi.image-build.builder-unreachable"
            )
        )
    ),
    "g5": Scenario(
        _ok(
            composition=lambda: domain_failed(
                "composition", "sdi.composition.scoped-rejection"
            )
        )
    ),
    "g6": Scenario(
        _ok(
            cv=lambda: adapter_failed(
                "cv",
                "sdi.stage.deadline-exceeded",
                conclusion="timed_out",
                termination="timed_out",
            )
        )
    ),
    "g7": Scenario(
        _ok(
            cv=lambda: adapter_failed(
                "cv",
                "sdi.run.deadline-exceeded",
                conclusion="timed_out",
                termination="timed_out",
            )
        )
    ),
    "g8": Scenario(OK, run_deadline_expired=True),
    "g9-no-node": Scenario(OK, run_deadline_expired=True),
    "g9-killed-cv": Scenario(OK, run_deadline_expired=True),
    "g9-race": Scenario(OK, lost=frozenset({"cv"}), run_deadline_expired=True),
    "g10": Scenario({}, run_deadline_expired=True),
    "g11": Scenario(OK),
    "g12": Scenario(OK),
    "g13": Scenario(OK),
    "g14": Scenario(OK),
    "g15": Scenario(
        {
            "composition": fixture_composition,
            "image_build": fixture_images,
            "cv": lambda: skipped("cv", "sdi.dependency.fixture-evidence"),
        }
    ),
    "g15b": Scenario(
        _ok(
            image_build=lambda: skipped(
                "image_build", "sdi.dependency.implemented-evidence", mode="fixture"
            )
        )
    ),
    "g16": Scenario(
        _ok(
            cv=lambda: adapter_failed(
                "cv", "sdi.adapter.process-lost", termination="lost"
            )
        )
    ),
    "g17": Scenario(_ok(cv=lambda: adapter_failed("cv", "sdi.adapter.nonzero-exit"))),
    "g18": Scenario(
        _ok(
            image_build=lambda: adapter_failed(
                "image_build", "sdi.adapter.schema-mismatch", exit_code=0
            )
        )
    ),
    "g19": Scenario(
        _ok(
            composition=lambda: adapter_failed(
                "composition",
                "sdi.run.deadline-exceeded",
                conclusion="timed_out",
                termination="timed_out",
            )
        ),
        limit_min=1,
    ),
    "g20": Scenario(OK),
    "g21": Scenario(
        _ok(
            image_build=lambda: images_built(
                failed={PLACEMENTS[2].service_id: "build-failed"}
            ),
            cv=cv_not_evaluated,
            cd=_cd_failed("CrashLoopBackOff"),
        )
    ),
}


def golden_rows(name: str) -> list[PipelineEventRow]:
    """Drive `build_row` through one golden, line by line, as the runtime would.

    The golden gives only what a caller owns, each line's Stage and step.
    `build_row` computes every status, `failure_reason` and field the checker
    then compares with the golden.
    """
    scenario = SCENARIOS[name]
    lines = golden(name).lines
    scope = (
        "run"
        if any(line.projection.stage is None for line in lines)
        else "single_stage"
    )
    held: list[StageExecution] = []
    rows: list[PipelineEventRow] = []
    for index, golden_line in enumerate(lines):
        stage = golden_line.projection.stage
        step = cast("StepCode", golden_line.projection.step)
        executions = list(held)
        head: LineHead
        if stage is None and step == "run-accepted":
            executions = []
            head = EventLine(None, step)
        elif stage is None:
            kept = [
                item
                for item in held
                if item.envelope.correlation.stage not in scenario.lost
            ]
            executions = finalized(
                kept, run_deadline_expired=scenario.run_deadline_expired
            )
            head = EventLine(None, step)
        elif step == "stage-finished":
            head = StageFinished(scenario.finished[stage]())
            held.append(head.execution)
        elif step in {"adapter-running", "work-limit-reached"}:
            limit = scenario.limit_min or STAGES[stage].grant.work_limit_seconds // 60
            head = EventLine(stage, step, {"limit_min": limit})
        else:
            head = EventLine(stage, step)
        rows.append(
            build(
                executions,
                line=head,
                scope=scope,
                emitted_at=EMITTED_AT + timedelta(seconds=index),
            )
        )
    return rows


def golden_console(name: str, *, noise: bool = True) -> bytes:
    """Return the console of a golden's rows, written by `write_event`."""
    return console(golden_rows(name), noise=noise)

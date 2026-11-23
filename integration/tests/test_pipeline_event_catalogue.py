"""The pipeline-event step catalogue."""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

import pytest

from sdi_pipeline_integration._cv_result import REQUIREMENTS, StepStatus
from sdi_pipeline_integration._domain_contracts import (
    FAILED_IMAGE_REASONS,
    K8S_REASONS,
    UNEVALUATED_REASONS,
)
from sdi_pipeline_integration._pipeline_event_catalogue import (
    KILLED_PREFIXES,
    PIPELINE_STATUSES,
    SDI_LINE_MAX_BYTES,
    SLOT_TYPES,
    STAGE_PATHS,
    STEP_BY_CODE,
    STEP_CODES,
    STEPS,
    SlotName,
    StepCode,
    template_slots,
)
from sdi_pipeline_integration._pipeline_events import render_heartbeat, render_message
from sdi_pipeline_integration._stage_registry import STAGE_ORDER, STAGES

if TYPE_CHECKING:
    from sdi_pipeline_integration._pipeline_event_catalogue import SlotValue, Step
    from sdi_pipeline_integration._stage_registry import StageName

FIX = "fix the row in _pipeline_event_catalogue.STEPS"
SPEC_RUNTIME_STEPS = {
    "run-accepted",
    "run-finished",
    "stage-started",
    "adapter-running",
    "work-limit-reached",
    "run-deadline-reached",
    "checking-output",
    "publishing",
    "stage-finished",
}
SPEC_ADAPTER_STEPS: dict[StageName, set[str]] = {
    "composition": {"discovery", "generation", "assessment"},
    "image_build": {"builder-check", "building", "image-built", "image-failed"},
    "cv": {
        "screening",
        "service-unfit",
        "leftover-cleanup",
        "sim-cached",
        "sim-build",
        "pull",
        "pull-blocked",
        "start",
        "mission",
        "mission-setup-done",
        "mission-step",
        "collecting-result",
        "teardown",
    },
    "cd": {
        "nodes-wait",
        "nodes-ready",
        "applying",
        "pod-running",
        "pod-not-running",
        "settling",
        "judging",
        "cleanup",
    },
}


def _maximum_value(name: SlotName) -> SlotValue:
    slot = SLOT_TYPES[name]
    match slot.kind:
        case "integer":
            return int("9" * slot.max_length)
        case "enum":
            return max(slot.values, key=len)
        case "identifier":
            return "a" * slot.max_length


def _longest_stage(step: Step) -> StageName | None:
    match step.scope:
        case "run":
            return None
        case "every-stage":
            return max(STAGE_ORDER, key=lambda name: len(STAGES[name].event_label))
        case stage:
            return stage


def _longest_prefix(stage: StageName | None) -> str:
    longest_code = max(STEP_CODES, key=len)
    if stage is None:
        return f"[sdi] C-00 RUN > {longest_code}  "
    tag = max((STAGES[name].event_tag for name in STAGE_ORDER), key=len)
    status = max(PIPELINE_STATUSES, key=len)
    return f"[sdi] C-00 {tag} > {longest_code}  {status}  "


def _forms() -> list[tuple[StepCode, frozenset[SlotName], bool]]:
    return [
        *((step.code, slots, False) for step in STEPS for slots in step.form_slots),
        *(
            (step.code, template_slots(step.heartbeat), True)
            for step in STEPS
            if step.heartbeat is not None
        ),
    ]


def test_step_codes_match_the_spec_tables() -> None:
    spec_codes = SPEC_RUNTIME_STEPS.union(*SPEC_ADAPTER_STEPS.values())
    assert set(STEP_CODES) == spec_codes, f"catalogue differs from spec 6; {FIX}"
    assert len(STEP_CODES) == len(STEPS), f"a step code repeats; {FIX}"


def test_step_code_type_lists_the_catalogue_codes() -> None:
    assert set(get_args(StepCode.__value__)) == set(STEP_CODES), (
        "StepCode and STEPS disagree; list every catalogue code in StepCode"
    )


def test_slot_name_type_lists_the_typed_slots() -> None:
    assert set(get_args(SlotName.__value__)) == set(SLOT_TYPES), (
        "SlotName and SLOT_TYPES disagree; type every slot name once"
    )


def test_every_template_names_only_typed_slots() -> None:
    for step in STEPS:
        for template in (*step.forms, *filter(None, (step.heartbeat,))):
            template_slots(template)


def test_untyped_template_slot_is_refused() -> None:
    with pytest.raises(ValueError, match="untyped slots"):
        template_slots("{Stage} waited {minutes} min")


def test_every_step_has_forms_with_distinct_slot_sets() -> None:
    for step in STEPS:
        assert len(set(step.form_slots)) == len(step.forms), (
            f"{step.code}: two forms take the same slots; {FIX}"
        )


@pytest.mark.parametrize(("code", "slots", "heartbeat"), _forms())
def test_every_template_fits_1024_bytes_with_maximum_length_slots(
    code: StepCode,
    slots: frozenset[SlotName],
    *,
    heartbeat: bool,
    capfd: pytest.CaptureFixture[str],
) -> None:
    step = STEP_BY_CODE[code]
    stage = _longest_stage(step)
    values: dict[SlotName, SlotValue] = {
        name: _maximum_value(name) for name in slots if name != "Stage"
    }
    if heartbeat:
        assert stage is not None
        message = render_heartbeat(code, stage=stage, slots=values)
    else:
        message = render_message(code, stage=stage, slots=values)
    line = _longest_prefix(stage) + message
    assert capfd.readouterr().err == "", f"{code}: maximum-length slots fell back"
    assert all(str(value) in message for value in values.values()), (
        f"{code}: a slot value is missing from {message!r}"
    )
    assert len(line.encode()) <= SDI_LINE_MAX_BYTES, (
        f"{code}: {len(line.encode())} bytes with maximum-length slots"
    )


def test_runtime_steps_are_the_run_level_and_every_stage_rows() -> None:
    runtime = {step.code for step in STEPS if step.emitter == "runtime"}
    assert runtime == SPEC_RUNTIME_STEPS, f"emitter differs from spec 6; {FIX}"


@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_adapter_steps_belong_to_the_stage_their_table_names(
    stage: StageName,
) -> None:
    codes = {
        step.code
        for step in STEPS
        if step.emitter == "adapter" and step.allows_stage(stage)
    }
    assert codes == SPEC_ADAPTER_STEPS[stage], f"{stage}: wrong adapter steps; {FIX}"


def test_run_steps_allow_only_run_level_lines() -> None:
    for code in ("run-accepted", "run-finished"):
        step = STEP_BY_CODE[code]
        assert step.allows_stage(None)
        assert not any(step.allows_stage(stage) for stage in STAGE_ORDER)


def test_heartbeat_steps_are_the_four_long_steps() -> None:
    heartbeats = {step.code for step in STEPS if step.heartbeat is not None}
    assert heartbeats == {"generation", "sim-build", "building", "mission"}


def test_kinds_follow_the_spec_tables() -> None:
    items = {step.code for step in STEPS if step.kind == "item"}
    results = {step.code for step in STEPS if step.kind == "result"}
    assert items == {
        "image-built",
        "image-failed",
        "service-unfit",
        "pull",
        "pull-blocked",
        "mission-step",
        "pod-running",
        "pod-not-running",
    }
    assert results == {"mission-setup-done", "nodes-ready"}


def test_stage_paths_publish_after_spawn_and_end_with_stage_finished() -> None:
    for path in STAGE_PATHS.values():
        assert path[-1] == "stage-finished", f"{path} does not end the Stage"
        if path[0] == "stage-started":
            assert path[-2] == "publishing", f"{path} skips publishing"
    assert STAGE_PATHS["pre-spawn-skip"] == ("stage-finished",)
    assert all(
        STEP_BY_CODE[code].emitter == "runtime"
        for path in STAGE_PATHS.values()
        for code in path
    )


def test_killed_prefixes_never_end_a_stage() -> None:
    assert all("stage-finished" not in prefix for prefix in KILLED_PREFIXES)
    assert ("stage-started", "adapter-running") in KILLED_PREFIXES


def test_enum_slot_values_come_from_the_domain_vocabularies() -> None:
    assert SLOT_TYPES["image_reason"].values == FAILED_IMAGE_REASONS
    assert SLOT_TYPES["k8s_reason"].values == K8S_REASONS
    assert SLOT_TYPES["mission_step"].values == REQUIREMENTS
    assert set(SLOT_TYPES["unfit_reason"].values) <= set(UNEVALUATED_REASONS)
    assert set(SLOT_TYPES["pull_reason"].values) == {
        "image-unavailable",
        "unsupported-command",
    }
    assert set(SLOT_TYPES["mission_status"].values) <= set(get_args(StepStatus))
    assert set(SLOT_TYPES["search"].values) == {"lexical", "lexical+embeddings"}
    assert K8S_REASONS == (
        "CrashLoopBackOff",
        "ErrImagePull",
        "ImagePullBackOff",
        "Unschedulable",
        "other",
    )


def test_stage_labels_and_tags_follow_the_line_grammar() -> None:
    assert [STAGES[name].event_label for name in STAGE_ORDER] == [
        "Composition",
        "Image build",
        "CV",
        "CD",
    ]
    assert [STAGES[name].event_tag for name in STAGE_ORDER] == [
        "COMPOSITION",
        "IMAGE-BUILD",
        "CV",
        "CD",
    ]

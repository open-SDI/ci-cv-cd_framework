"""A pipeline-event row's result fields, from result files and live reports."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sdi_pipeline_integration._cv_result import REQUIREMENTS
from sdi_pipeline_integration._domain_contracts import image_tag
from sdi_pipeline_integration._pipeline_event_rows import EventLine, StageFinished
from tests._pipeline_event_world import (
    PLACEMENTS,
    Placement,
    adapter_failed,
    attempt,
    build,
    built,
    cd_deployed,
    composition_ok,
    cv_not_evaluated,
    cv_passed,
    cv_service_absent,
    deployment_result,
    domain_failed,
    fixture_images,
    image_build_result,
    images_built,
)
from tests._pipeline_events import EXECUTION_ID
from tests._stage_attempts import DURATION_MS, reloaded, skipped

if TYPE_CHECKING:
    from sdi_pipeline_integration._stage_runtime import StageExecution

MISSION_RUNNING = EventLine("cv", "mission")
SETUP_DONE = EventLine("cv", "mission-setup-done", {"setup_status": "passed", "s": 41})


def _step(step: str, status: str | None, index: int) -> EventLine:
    slots: dict[str, str | int] = {"mission_step": step, "i": index, "n": 8}
    if status is not None:
        slots |= {"mission_status": status, "s": 30 * index}
    return EventLine("cv", "mission-step", slots)  # pyright: ignore[reportArgumentType]


def _ci_held() -> list[StageExecution]:
    return [composition_ok(), images_built()]


def test_ci_duration_is_null_until_ci_ends_then_sums_both_halves() -> None:
    composition = composition_ok()

    assert build(line=StageFinished(composition)).ci_duration_ms is None
    row = build([composition], line=StageFinished(images_built()))
    assert row.ci_duration_ms == DURATION_MS["composition"] + DURATION_MS["image_build"]


def test_ci_duration_is_compositions_alone_when_image_build_had_no_process() -> None:
    skip = skipped("image_build", "sdi.dependency.implemented-evidence", mode="fixture")

    row = build([composition_ok()], line=StageFinished(skip))

    assert row.ci_duration_ms == DURATION_MS["composition"]


def test_refs_name_the_archived_files_a_stage_accepted() -> None:
    held = [composition_ok(), images_built(), cv_passed()]

    row = build(held, line=StageFinished(cd_deployed()))

    assert (
        row.blueprint_ref,
        row.deployment_schema_ref,
        row.image_build_result_ref,
        row.validation_evidence_ref,
        row.deployment_result_ref,
    ) == (
        "stages/composition/composition-blueprint.json",
        "stages/composition/deployment-schema.json",
        "stages/image-build/image-build-result.json",
        "stages/cv/validation-evidence.json",
        "stages/cd/deployment-result.json",
    )


def test_a_refused_composition_has_no_refs_or_image_fields() -> None:
    refused = domain_failed("composition", "sdi.composition.scoped-rejection")

    row = build(line=StageFinished(refused))

    assert (row.blueprint_ref, row.deployment_schema_ref) == (None, None)
    assert (row.image_ref, row.images_total, row.platform_status) == (None, None, None)
    assert row.ci_duration_ms == DURATION_MS["composition"]


def test_image_fields_list_built_images_by_service_id_with_the_shared_tag() -> None:
    row = build([composition_ok()], line=StageFinished(images_built()))

    ordered = sorted(PLACEMENTS, key=lambda item: item.service_id)
    assert row.image_ref == [
        image_tag(
            service_id=item.service_id,
            artifact_id=item.artifact_id,
            execution_id=EXECUTION_ID,
        )
        for item in ordered
    ]
    assert row.image_digest == [built(item.service_id)["digest"] for item in ordered]
    assert (row.images_built, row.images_total, row.image_failures) == (3, 3, [])
    assert (row.platform_status, row.detected_platform) == ("MATCH", "linux/arm64")
    assert row.image_archive is None


def test_failed_images_are_counted_and_listed() -> None:
    failed = {PLACEMENTS[0].service_id: "no-recipe"}

    row = build([composition_ok()], line=StageFinished(images_built(failed=failed)))

    assert (row.images_built, row.images_total) == (2, 3)
    assert [item.model_dump() for item in row.image_failures or []] == [
        {"service_id": PLACEMENTS[0].service_id, "reason": "no-recipe"}
    ]
    assert row.image_build_status == "SUCCEEDED"
    assert len(row.image_ref or []) == 2


def test_a_fixture_image_build_has_empty_image_fields() -> None:
    fixture = fixture_images()

    row = build(line=StageFinished(fixture))

    assert (row.image_ref, row.image_digest, row.images_built, row.image_failures) == (
        [],
        [],
        0,
        [],
    )
    assert (row.platform_status, row.detected_platform) == (None, None)


def test_without_a_deployment_schema_the_tags_and_platforms_are_null() -> None:
    row = build(line=StageFinished(images_built()), scope="single_stage")

    assert (row.image_ref, row.platform_status, row.detected_platform) == (
        None,
        None,
        None,
    )
    assert row.image_digest is not None
    assert (row.images_built, row.images_total) == (3, 3)


def test_an_image_without_its_hosts_chip_is_a_mismatch() -> None:
    amd64_only = image_build_result()
    images = amd64_only["images"]
    images[0] = built(PLACEMENTS[0].service_id) | {
        "platforms": [built(PLACEMENTS[0].service_id)["platforms"][0]]
    }
    result = attempt("image_build", documents={"image_build_result": amd64_only})

    row = build([composition_ok()], line=StageFinished(result))

    assert row.platform_status == "MISMATCH"
    assert row.detected_platform is None


def test_cv_fields_come_from_the_evidence_on_cvs_stage_finished() -> None:
    row = build(_ci_held(), line=StageFinished(cv_passed()))

    assert row.cv_duration_ms == DURATION_MS["cv"]
    assert row.cv_platform == "linux/amd64"
    assert (
        row.cv_services_participating,
        row.cv_services_blocked,
        row.cv_services_total,
    ) == (3, 0, 3)
    assert row.cv_service_issues == []
    assert row.mission_setup == "passed"
    assert [entry.id for entry in row.mission_steps or []] == list(REQUIREMENTS)
    assert all(entry.status is not None for entry in row.mission_steps or [])


def test_cv_issues_list_absent_services_and_count_blocked_ones() -> None:
    not_evaluated = cv_not_evaluated()

    row = build(_ci_held(), line=StageFinished(not_evaluated))

    assert (
        row.cv_services_participating,
        row.cv_services_blocked,
        row.cv_services_total,
    ) == (0, 2, 3)
    assert [item.model_dump() for item in row.cv_service_issues or []] == [
        {
            "service_id": PLACEMENTS[2].service_id,
            "participation": "not_evaluated",
            "reason": "image-unavailable",
        }
    ]
    assert (row.mission_setup, row.mission_steps) == (None, None)


def test_a_service_that_did_not_participate_is_listed() -> None:
    absent = cv_service_absent()

    row = build(_ci_held(), line=StageFinished(absent))

    assert [item.participation for item in row.cv_service_issues or []] == [
        "did_not_participate"
    ]
    assert row.cv_services_participating == 2


def test_cd_fields_come_from_the_deployment_result() -> None:
    held = [*_ci_held(), cv_passed()]

    row = build(held, line=StageFinished(cd_deployed()))

    assert row.cd_duration_ms == DURATION_MS["cd"]
    assert (
        row.cd_placements_deployed,
        row.cd_placements_blocked,
        row.cd_placements_total,
    ) == (3, 0, 3)
    assert row.placement_issues == []


def test_a_failed_placement_without_a_kubernetes_reason_reads_other() -> None:
    result = deployment_result(failed={PLACEMENTS[1].service_id: None})
    failed = domain_failed("cd", "sdi.cd.failed", {"deployment_result": result})

    row = build([*_ci_held(), cv_passed()], line=StageFinished(failed))

    assert [item.model_dump() for item in row.placement_issues or []] == [
        {
            "service_id": PLACEMENTS[1].service_id,
            "host": PLACEMENTS[1].host,
            "status": "failed",
            "reason": "other",
        }
    ]
    assert row.cd_placements_deployed == 2


def test_undeployed_placements_list_their_gap_and_count_blocked_ones() -> None:
    result = deployment_result()
    result["domain_outcome"] = "not_evaluated"
    del result["manifest"]
    result["placements"] = [
        {
            "service_id": "nav2-localization",
            "status": "not_evaluated",
            "reason": "blocked",
        },
        {
            "service_id": "face-recog",
            "status": "not_evaluated",
            "reason": "unresolved-placement",
        },
    ]
    undeployed = attempt(
        "cd",
        domain="not_evaluated",
        reason=("domain", "sdi.cd.not-evaluated"),
        documents={"deployment_result": result},
    )

    row = build([*_ci_held(), cv_passed()], line=StageFinished(undeployed))

    assert (row.cd_placements_deployed, row.cd_placements_blocked) == (0, 1)
    assert [item.model_dump() for item in row.placement_issues or []] == [
        {
            "service_id": "face-recog",
            "host": None,
            "status": "not_evaluated",
            "reason": "unresolved-placement",
        }
    ]


def test_mission_fields_are_null_until_the_first_mission_report() -> None:
    row = build(_ci_held(), line=MISSION_RUNNING, live_items=[MISSION_RUNNING])

    assert (row.mission_setup, row.mission_steps) == (None, None)


def test_setup_done_fills_mission_setup_and_eight_unreported_steps() -> None:
    row = build(_ci_held(), line=SETUP_DONE, live_items=[MISSION_RUNNING, SETUP_DONE])

    assert row.mission_setup == "passed"
    assert [(entry.id, entry.status) for entry in row.mission_steps or []] == [
        (step, None) for step in REQUIREMENTS
    ]


def test_mission_step_reports_fill_their_steps_and_scripted_reads_scripted() -> None:
    reports = [
        SETUP_DONE,
        _step("secure-book", None, 1),
        _step("accept-spoken-request", "passed", 2),
        _step("accept-spoken-request", "failed", 2),
    ]

    row = build(_ci_held(), line=reports[-1], live_items=reports)

    statuses = {entry.id: entry.status for entry in row.mission_steps or []}
    assert statuses["secure-book"] == "scripted"
    assert statuses["accept-spoken-request"] == "passed", "the first report wins"
    assert statuses["return-to-origin"] is None
    assert (row.cv_duration_ms, row.cv_services_total) == (None, None)


def test_the_head_alone_is_not_a_live_item() -> None:
    report = _step("accept-spoken-request", "passed", 2)

    row = build(_ci_held(), line=report, live_items=[])

    assert row.mission_steps is None


def test_unfit_and_blocked_services_are_live_cv_issues() -> None:
    reports = [
        EventLine(
            "cv",
            "service-unfit",
            {"service_id": "face-recog", "unfit_reason": "needs-gpu-or-device"},
        ),
        EventLine(
            "cv",
            "pull-blocked",
            {
                "service_id": "nav2-navigation",
                "pull_reason": "image-unavailable",
                "i": 2,
                "n": 3,
            },
        ),
    ]

    row = build(
        _ci_held(),
        line=EventLine(
            "cv", "pull", {"service_id": "nav2-localization", "i": 3, "n": 3}
        ),
        live_items=reports,
    )

    assert [item.model_dump() for item in row.cv_service_issues or []] == [
        {
            "service_id": "face-recog",
            "participation": "not_evaluated",
            "reason": "needs-gpu-or-device",
        },
        {
            "service_id": "nav2-navigation",
            "participation": "not_evaluated",
            "reason": "image-unavailable",
        },
    ]


def test_a_pod_not_running_report_is_a_live_placement_issue() -> None:
    report = EventLine(
        "cd",
        "pod-not-running",
        {
            "service_id": "nav2-navigation",
            "host": "waffle",
            "k8s_reason": "ImagePullBackOff",
            "i": 2,
            "n": 3,
        },
    )

    row = build([*_ci_held(), cv_passed()], line=report, live_items=[report])

    assert [item.model_dump() for item in row.placement_issues or []] == [
        {
            "service_id": "nav2-navigation",
            "host": "waffle",
            "status": "failed",
            "reason": "ImagePullBackOff",
        }
    ]
    assert row.cd_placements_total is None


def test_live_items_count_only_for_the_running_stage() -> None:
    cv_reports = [SETUP_DONE, _step("accept-spoken-request", "passed", 2)]
    cd_report = EventLine(
        "cd",
        "pod-not-running",
        {
            "service_id": "face-recog",
            "host": "orin",
            "k8s_reason": "other",
            "i": 1,
            "n": 3,
        },
    )

    cd_line = build(
        _ci_held(), line=EventLine("cd", "stage-started"), live_items=cv_reports
    )
    cv_line = build(
        _ci_held(), line=EventLine("cv", "teardown"), live_items=[cd_report]
    )
    finished = build(
        _ci_held(), line=EventLine(None, "run-finished"), live_items=cv_reports
    )

    assert (cd_line.mission_setup, cd_line.mission_steps) == (None, None)
    assert cv_line.placement_issues is None
    assert finished.mission_steps is None


def test_live_items_are_ignored_for_a_stage_that_has_an_attempt() -> None:
    held = [*_ci_held(), cv_passed()]
    reports = [SETUP_DONE, _step("accept-spoken-request", "failed", 2)]

    row = build(held, line=EventLine("cv", "publishing"), live_items=reports)

    statuses = {entry.id: entry.status for entry in row.mission_steps or []}
    assert statuses["accept-spoken-request"] == "passed"


def test_cv_without_evidence_goes_back_to_null_on_stage_finished() -> None:
    reports = [SETUP_DONE, _step("accept-spoken-request", "passed", 2)]
    timed_out = adapter_failed(
        "cv",
        "sdi.stage.deadline-exceeded",
        conclusion="timed_out",
        termination="timed_out",
    )

    running = build(
        _ci_held(),
        line=EventLine("cv", "work-limit-reached", {"limit_min": 45}),
        live_items=reports,
    )
    finished = build(_ci_held(), line=StageFinished(timed_out), live_items=reports)

    assert running.mission_steps is not None
    assert (finished.mission_setup, finished.mission_steps) == (None, None)
    assert finished.validation_evidence_ref is None
    assert finished.cv_duration_ms == DURATION_MS["cv"]


def test_a_corrupt_result_file_reads_as_absent_but_keeps_its_ref() -> None:
    passed = cv_passed()
    corrupt = replace(passed, files=dict.fromkeys(passed.files, b"{not json"))

    row = build(_ci_held(), line=StageFinished(corrupt))

    assert row.cv_status == "SUCCEEDED"
    assert row.validation_evidence_ref == "stages/cv/validation-evidence.json"
    assert (row.cv_platform, row.mission_steps) == (None, None)


def test_a_document_is_read_only_from_its_accepted_path() -> None:
    passed = cv_passed()
    (path,) = passed.files
    moved = replace(passed, files={f"{path}.extra": passed.files[path]})

    row = build(_ci_held(), line=StageFinished(moved))

    assert row.cv_platform is None


def test_attempts_round_trip_the_production_loader() -> None:
    composition = composition_ok()

    assert reloaded(composition) == composition
    assert set(composition.outputs) == {"composition_blueprint", "deployment_schema"}
    assert set(cv_passed().files) == {"outputs/validation-evidence.json"}
    assert cv_passed().outputs == {}


def test_placements_on_another_host_keep_their_own_chip() -> None:
    placements = (*PLACEMENTS, Placement("vosk-speech", "jazzy-debs", "orin"))

    row = build(
        [composition_ok(placements)],
        line=StageFinished(images_built(placements)),
    )

    assert row.platform_status == "MATCH"
    assert row.images_total == 4

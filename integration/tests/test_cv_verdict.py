"""CV's verdict on every passed, failed and not-evaluated run, and its CV errors."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Literal, get_args

import pytest

from sdi_pipeline_integration._adapter_kit import SUCCEEDED, Outcome, serve
from sdi_pipeline_integration._cv_adapter import validate
from sdi_pipeline_integration._cv_run import RunError
from sdi_pipeline_integration._cv_verdict import (
    CvError,
    Judgement,
    assess,
)
from sdi_pipeline_integration._domain_contracts import (
    NonParticipatingService,
    ParticipatingService,
    UnevaluatedService,
)
from tests._cv_builders import (
    IMAGE,
    POSE,
    STRING,
    TWIST,
    amd64_digest,
    blocked,
    composed,
    errored,
    evidence_of,
    exited,
    finished,
    mission,
    never,
    run_is_red_for,
    runner,
    unstartable,
)
from tests._cv_evidence import WAITED_ON
from tests._cv_recordings import recorded
from tests._cv_stage import CvRun, CvStage, description, refusing_engine_url

if TYPE_CHECKING:
    from pathlib import Path

    from sdi_pipeline_integration._cv_run import BlockingCause
    from sdi_pipeline_integration._cv_verdict import Composed, Runner
    from sdi_pipeline_integration._stage_contracts import ReasonCategory
    from tests._cv_builders import Requested, RunFor

LOCALIZATION = composed("nav2-localization", {"/amcl_pose": POSE, "/tf": "tf"})
NAVIGATION = composed("nav2-navigation", {"/cmd_vel": TWIST})
SPEECH = composed("vosk-speech", {"/speech/text": STRING})
FACE = composed("opencv-face-recognition", {"/face_recognition/name": STRING})
CAMERA = composed("v4l2-camera", {"/image": IMAGE}, devices=["camera"])
SERVICES = (LOCALIZATION, NAVIGATION, SPEECH, CAMERA)


def _judged(services: tuple[Composed, ...], run: Runner) -> Judgement:
    judgement = assess(services, run)
    assert isinstance(judgement, Judgement), judgement
    evidence = evidence_of(judgement)
    assert evidence.domain_outcome == judgement.outcome.domain_outcome
    return judgement


def test_cv_every_service_participating_in_a_completed_mission_succeeds() -> None:
    seen: list[Requested] = []
    judgement = _judged(SERVICES, runner(finished(), seen))

    assert judgement.outcome is SUCCEEDED
    assert [item.participation for item in judgement.services] == [
        "participated",
        "participated",
        "participated",
        "participated_loosened",
    ]
    participating = [
        item for item in judgement.services if isinstance(item, ParticipatingService)
    ]
    assert [item.image_digest for item in participating] == [
        amd64_digest(item.service_id) for item in SERVICES
    ]
    assert participating[0].counting_topics[0].model_dump() == {
        "topic": "/amcl_pose",
        "messages": 5,
    }
    assert participating[3].counting_topics == []
    assert not run_is_red_for(judgement.outcome)
    ((placements, counting),) = seen
    assert placements[0].reference == (
        f"registry:5000/sdi/nav2-localization@{amd64_digest('nav2-localization')}"
    )
    assert [(item.topic, item.service_id) for item in counting] == [
        ("/amcl_pose", "nav2-localization"),
        ("/cmd_vel", "nav2-navigation"),
        ("/speech/text", "vosk-speech"),
    ]


@pytest.mark.parametrize(
    ("run", "summary", "service"),
    [
        (
            finished(states={"vosk-speech": exited(137)}),
            "vosk-speech did not participate: container-exited (exit 137).",
            {"reason": "container-exited", "exit_code": 137},
        ),
        (
            finished(states={"vosk-speech": exited(0)}),
            "vosk-speech did not participate: container-exited (exit 0).",
            {"reason": "container-exited", "exit_code": 0},
        ),
        (
            finished(states={"vosk-speech": unstartable("no space left")}),
            "vosk-speech did not participate: failed-to-start.",
            {"reason": "failed-to-start", "detail": "no space left"},
        ),
        (
            finished(counts={"/speech/text": 0}),
            "vosk-speech did not participate: no-message.",
            {"reason": "no-message"},
        ),
    ],
    ids=["exited-137", "exited-0", "failed-to-start", "no-message"],
)
def test_cv_a_service_that_did_not_participate_fails_the_composition(
    run: RunFor, summary: str, service: dict[str, object]
) -> None:
    judgement = _judged(SERVICES, runner(run))

    assert judgement.outcome == Outcome(
        "failed", "succeeded", "failed", "domain", summary
    )
    speech = judgement.services[2]
    assert isinstance(speech, NonParticipatingService)
    assert speech.model_dump(exclude_none=True, include=set(service)) == service
    assert (speech.image_digest is None) == (service["reason"] == "failed-to-start")
    assert run_is_red_for(judgement.outcome)


def test_cv_a_loosened_service_that_exited_did_not_participate() -> None:
    judgement = _judged(SERVICES, runner(finished(states={"v4l2-camera": exited(1)})))

    assert judgement.outcome.domain_outcome == "failed"
    camera = judgement.services[3]
    assert isinstance(camera, NonParticipatingService)
    assert (camera.reason, camera.exit_code) == ("container-exited", 1)


def test_cv_a_strict_service_counts_on_any_one_of_its_outputs() -> None:
    nav = composed("nav2-localization", {"/amcl_pose": POSE, "/map": "grid"})
    judgement = _judged((nav,), runner(finished(counts={"/map": 0})))

    assert judgement.outcome is SUCCEEDED


def test_cv_the_summary_names_the_first_service_in_blueprint_order() -> None:
    run = finished(
        states={"vosk-speech": exited(2), "nav2-navigation": unstartable()},
        scenario_mission=mission("verify-recipient-face"),
    )

    judgement = _judged(SERVICES, runner(run))

    assert judgement.outcome.domain_outcome == "failed"
    assert isinstance(judgement.outcome, Outcome)
    assert judgement.outcome.summary == (
        "nav2-navigation did not participate: failed-to-start."
    )


UNMET = [
    (requirement_id, status)
    for requirement_id, waited_on in WAITED_ON.items()
    if waited_on is not None
    for status in ("failed", "timed_out")
]


@pytest.mark.parametrize(("requirement_id", "status"), UNMET)
def test_cv_an_unmet_step_fails_the_mission_by_name(
    requirement_id: str, status: Literal["failed", "timed_out"]
) -> None:
    run = finished(scenario_mission=mission(requirement_id, status))

    judgement = _judged(SERVICES, runner(run))

    words = status.replace("_", " ")
    assert judgement.outcome == Outcome(
        "failed",
        "succeeded",
        "failed",
        "domain",
        f"Mission step {requirement_id} {words} on {WAITED_ON[requirement_id]}.",
    )
    assert judgement.mission is not None
    assert [item.status for item in judgement.mission.steps].count(status) == 1
    assert all(isinstance(item, ParticipatingService) for item in judgement.services)


def test_cv_a_setup_timeout_fails_the_mission() -> None:
    run = finished(scenario_mission=mission(setup="timed_out"))

    judgement = _judged(SERVICES, runner(run))

    assert isinstance(judgement.outcome, Outcome)
    assert judgement.outcome.summary == "The mission setup timed out."
    assert judgement.mission is not None
    assert judgement.mission.setup == "timed_out"
    assert {item.status for item in judgement.mission.steps} == {"not_reached"}


@pytest.mark.parametrize(
    ("causes", "summary"),
    [
        (
            {"vosk-speech": "unsupported-command"},
            "vosk-speech was not evaluated: unsupported-command.",
        ),
        (
            {"vosk-speech": "unsupported-command", "v4l2-camera": "image-unavailable"},
            "v4l2-camera was not evaluated: image-unavailable.",
        ),
    ],
    ids=["unsupported-command", "image-unavailable-first"],
)
def test_cv_a_blocked_run_is_not_evaluated(
    causes: dict[str, BlockingCause], summary: str
) -> None:
    judgement = _judged(SERVICES, runner(blocked(causes)))

    assert judgement.outcome == Outcome(
        "not-evaluated", "succeeded", "not_evaluated", "dependency", summary
    )
    assert judgement.mission is None
    assert [
        item.reason
        for item in judgement.services
        if isinstance(item, UnevaluatedService)
    ] == [causes.get(item.service_id, "blocked") for item in SERVICES]
    assert not run_is_red_for(judgement.outcome)


def test_cv_an_unloadable_counting_type_is_not_evaluated() -> None:
    run = finished(unloadable={"/speech/text": "vosk_msgs/msg/Text"})

    judgement = _judged(SERVICES, runner(run))

    assert isinstance(judgement.outcome, Outcome)
    assert (
        judgement.outcome.summary == "vosk-speech was not evaluated: unloadable-type."
    )
    speech = judgement.services[2]
    assert isinstance(speech, UnevaluatedService)
    assert (speech.reason, speech.detail) == ("unloadable-type", "vosk_msgs/msg/Text")
    assert speech.image_digest == amd64_digest("vosk-speech")
    assert [
        item.reason
        for item in judgement.services
        if isinstance(item, UnevaluatedService) and item is not speech
    ] == ["blocked"] * 3


def test_cv_an_unloadable_type_on_no_counting_topic_is_an_invalid_result() -> None:
    error = assess(SERVICES, runner(finished(unloadable={"/other": "pkg/msg/T"})))

    assert isinstance(error, CvError)
    assert error.code == "invalid-result"


ERROR_CATEGORIES: dict[RunError, ReasonCategory] = {
    "sim-build-failed": "tool",
    "pull-failed": "dependency",
    "no-time-to-start": "tool",
    "start-failed": "tool",
    "orchestrator-timeout": "tool",
    "sim-not-running": "tool",
    "no-result": "tool",
    "result-too-large": "tool",
    "invalid-result": "tool",
}


@pytest.mark.parametrize("error", get_args(RunError))
def test_cv_a_run_error_is_a_cv_error(error: RunError) -> None:
    result = assess(SERVICES, runner(errored(error)))

    assert isinstance(result, CvError)
    assert result.outcome == Outcome(
        error,
        "failed",
        "not_evaluated",
        ERROR_CATEGORIES[error],
        result.outcome.summary,
    )
    assert run_is_red_for(result.outcome)


def _published_error(tmp_path: Path, run: CvRun, code: str, category: str) -> None:
    assert run.response.execution_conclusion == "failed"
    assert run.response.domain_outcome == "not_evaluated"
    assert run.response.reason is not None
    assert (run.response.reason.code, run.response.reason.category) == (
        f"sdi.cv.{code}",
        category,
    )
    assert run.response.produced_outputs == []
    assert run.evidence is None
    assert not (tmp_path / "candidate/outputs").exists()
    assert run.diagnostic.startswith(f"outcome: sdi.cv.{code}\n")


def test_cv_an_engine_that_does_not_answer_is_a_cv_error(tmp_path: Path) -> None:
    stage = CvStage(tmp_path)
    stage.write(
        {"vosk-speech": description("vosk-speech", {"/speech/text": STRING})},
        [
            {
                "service_id": "vosk-speech",
                "status": "built",
                "repository": "sdi/vosk-speech",
                "digest": amd64_digest("vosk-speech"),
                "platforms": [
                    {"architecture": "amd64", "digest": amd64_digest("vosk-speech")}
                ],
            }
        ],
    )

    run = stage.run(serve("cv", partial(validate, engine_url=refusing_engine_url())))

    _published_error(tmp_path, run, "engine-unreachable", "dependency")


def test_cv_evidence_over_its_grant_is_a_cv_error(tmp_path: Path) -> None:
    stage = CvStage(tmp_path)
    stage.write(
        {"whisper-server": description("whisper-server", devices=["nvidia-gpu"])},
        [
            {
                "service_id": "whisper-server",
                "status": "failed",
                "reason": "no-recipe",
                "detail": "the artifact names no recipe in the service folder",
            }
        ],
        evidence_max_bytes=64,
    )

    run = stage.run(serve("cv", partial(validate, engine_url=refusing_engine_url())))

    _published_error(tmp_path, run, "evidence-too-large", "tool")


RECORDED_SERVICES = {
    "waffle-pass": (LOCALIZATION, NAVIGATION, SPEECH, FACE),
    "burger-pass": (LOCALIZATION, NAVIGATION, SPEECH, FACE),
    "no-face": (LOCALIZATION, NAVIGATION, SPEECH),
}


@pytest.mark.parametrize(
    ("name", "summary"),
    [
        ("waffle-pass", None),
        ("burger-pass", None),
        (
            "no-face",
            "Mission step verify-recipient-face timed out on /face_recognition/name.",
        ),
    ],
)
def test_cv_recorded_results_map_to_their_verdicts(
    name: str, summary: str | None
) -> None:
    result = recorded(name)

    judgement = _judged(
        RECORDED_SERVICES[name],
        runner(finished(counts=result.counts, scenario_mission=result.mission)),
    )

    if summary is None:
        assert judgement.outcome is SUCCEEDED
        assert {item.participation for item in judgement.services} == {"participated"}
    else:
        assert isinstance(judgement.outcome, Outcome)
        assert judgement.outcome.summary == summary
        assert run_is_red_for(judgement.outcome)


def test_cv_a_composition_cv_cannot_evaluate_never_reaches_the_runner() -> None:
    gpu = composed("whisper-server", devices=["nvidia-gpu"])

    judgement = _judged((LOCALIZATION, gpu), never)

    assert isinstance(judgement.outcome, Outcome)
    assert judgement.outcome.summary == (
        "whisper-server was not evaluated: needs-gpu-or-device."
    )

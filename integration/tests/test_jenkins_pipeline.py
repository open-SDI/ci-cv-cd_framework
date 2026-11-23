"""Black-box checks for the public CLI seams used by Jenkins."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from typing import TYPE_CHECKING, Any

import pytest

from sdi_pipeline_integration._cd_deployment import manifest_yaml
from sdi_pipeline_integration._result_contracts import FIXTURE_NOTICE
from sdi_pipeline_integration._stage_registry import STAGES
from tests import _cd_evidence as cd
from tests._cv_evidence import (
    AMD64_DIGEST,
    FAILED,
    NOT_EVALUATED,
    SERVICE_IDS,
    SUCCEEDED,
)
from tests._cv_stage import description
from tests._fixture_inputs import (
    FIXTURE_SERVICE_REPOSITORY,
    RED_RUN_EXIT,
    write_fixture_composition_descriptor,
    write_scripted_adapter,
)
from tests._scripted_stages import (
    CD_IMAGES,
    CD_REASONS,
    COMPOSITION_CONFIG_ARGUMENTS,
    CV_IMAGES,
    CV_PLACEMENTS,
    CV_REASONS,
    DIGEST,
    EXECUTION_ID,
    SOURCE_ROOT,
    SUCCESS_REQUEST,
    baked_code_digest,
    common_arguments,
    execute_chain,
    fresh_image,
    git,
    implemented_upstream,
    jenkins_repository,
    run_preflight,
    select_evidence_adapter,
    write_response_adapter,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_distributed_fixture_chain_preserves_identity_and_exact_archive(
    tmp_path: Path,
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path, fixture_image_build=True)
    started_millis = str(int((time.time() - 1) * 1000))

    conclusion, result = execute_chain(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        tmp_path / "run",
        started_millis,
    )

    assert conclusion.returncode == RED_RUN_EXIT, conclusion.stderr
    assert result["execution_id"] == EXECUTION_ID
    assert [
        (
            attempt["correlation"]["stage"],
            attempt["execution_conclusion"],
            attempt["reason"]["code"],
        )
        for attempt in result["attempts"]
    ] == [
        ("composition", "succeeded", "sdi.fixture.composition-executed"),
        ("image_build", "succeeded", "sdi.fixture.image-build-executed"),
        ("cv", "skipped", "sdi.dependency.fixture-evidence"),
        ("cd", "skipped", "sdi.dependency.prerequisite-blocked"),
    ]
    assert {artifact["stage"] for artifact in result["artifacts"]} == {
        "composition",
        "image_build",
    }


def test_implemented_image_build_skips_fixture_composition(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)

    conclusion, result = execute_chain(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
    )

    assert conclusion.returncode == 1
    assert result["attempts"][1]["reason"]["code"] == "sdi.dependency.fixture-evidence"


def _execute_stage(
    repository: Path,
    commit_sha: str,
    stage: str,
    attempt_root: Path,
    supplied: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *common_arguments(repository, commit_sha, SUCCESS_REQUEST),
            *fresh_image(repository),
            *supplied,
            "--stage",
            stage,
            "--attempt-root",
            str(attempt_root),
            "--run-deadline-epoch-millis",
            str(int(time.time() * 1000) + 60_000),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_composition_without_a_checked_out_service_repository_claims_no_attempt(
    tmp_path: Path,
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    shutil.rmtree(repository / "service-repository")
    attempt = tmp_path / "attempt"

    executed = _execute_stage(
        repository, commit_sha, "composition", attempt, COMPOSITION_CONFIG_ARGUMENTS
    )

    assert executed.returncode == 2
    assert executed.stderr == (
        f"sdi-integration: {repository / 'service-repository'}:"
        " service repository is not a directory\n"
    )
    assert not attempt.exists()


COMPOSITION_GRANT_SECONDS = STAGES["composition"].grant.work_limit_seconds


def _execute_composition(
    repository: Path, commit_sha: str, attempt: Path, *work_limit: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *common_arguments(repository, commit_sha, SUCCESS_REQUEST),
            *fresh_image(repository),
            *COMPOSITION_CONFIG_ARGUMENTS,
            "--stage",
            "composition",
            "--attempt-root",
            str(attempt),
            *work_limit,
            "--run-deadline-epoch-millis",
            str(int(time.time() * 1000) + 60_000),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("work_limit", ["0", str(COMPOSITION_GRANT_SECONDS + 1)])
def test_a_work_limit_outside_the_grant_claims_no_attempt(
    tmp_path: Path, work_limit: str
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    attempt = tmp_path / "attempt"

    executed = _execute_composition(
        repository, commit_sha, attempt, "--work-limit-seconds", work_limit
    )

    assert executed.returncode == 2
    assert executed.stderr == (
        "sdi-integration: --work-limit-seconds must be between 1"
        f" and the composition Stage's granted {COMPOSITION_GRANT_SECONDS} seconds\n"
    )
    assert not attempt.exists()


CD_GRANT_SECONDS = STAGES["cd"].grant.work_limit_seconds


def test_a_cd_work_limit_too_short_for_its_node_wait_claims_no_attempt(
    tmp_path: Path,
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    attempt = tmp_path / "attempt"

    executed = _execute_stage(
        repository, commit_sha, "cd", attempt, ("--work-limit-seconds", "219")
    )

    assert executed.returncode == 2
    assert executed.stderr == (
        "sdi-integration: --work-limit-seconds must be between 220"
        f" and the cd Stage's granted {CD_GRANT_SECONDS} seconds\n"
    )
    assert not attempt.exists()


def test_the_cd_minimum_work_limit_is_not_refused(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)

    executed = _execute_stage(
        repository,
        commit_sha,
        "cd",
        tmp_path / "attempt",
        ("--work-limit-seconds", "220"),
    )

    assert "--work-limit-seconds" not in executed.stderr


def test_a_stage_without_a_work_limit_gets_its_full_grant(tmp_path: Path) -> None:
    repository, _ = jenkins_repository(tmp_path)
    recorded = tmp_path / "work-limit"
    adapter = tmp_path / "recording-composition-adapter"
    write_scripted_adapter(
        adapter,
        f"Path({str(recorded)!r}).write_text(str(request['work_limit_seconds']))\n",
    )
    write_fixture_composition_descriptor(
        repository, entrypoint=adapter, mode="implemented"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Select a recording composition adapter")

    executed = _execute_composition(
        repository, git(repository, "rev-parse", "HEAD"), tmp_path / "attempt"
    )

    assert executed.returncode == 0, executed.stderr
    assert recorded.read_text() == str(COMPOSITION_GRANT_SECONDS)


SUPPLIED_INPUTS = (
    (COMPOSITION_CONFIG_ARGUMENTS, "composition config"),
    (("--generator", "fixture"), "generator"),
)


@pytest.mark.parametrize(
    ("stage", "supplied", "label"),
    [
        (stage, supplied, label)
        for stage in list(STAGES)[1:]
        for supplied, label in SUPPLIED_INPUTS
    ],
)
def test_stages_not_granted_supplied_inputs_reject_them(
    tmp_path: Path,
    stage: str,
    supplied: tuple[str, ...],
    label: str,
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    attempt = tmp_path / "attempt"

    executed = _execute_stage(repository, commit_sha, stage, attempt, supplied)

    assert executed.returncode == 2
    assert f"the {stage} Stage is not granted a supplied {label}" in executed.stderr
    assert not attempt.exists()


def test_implemented_composition_runs_the_image_build_adapter(
    tmp_path: Path,
) -> None:
    repository, _commit_sha = jenkins_repository(
        tmp_path, service_repository=_described_fixture_services(tmp_path)
    )
    case = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
    adapter = tmp_path / "implemented-composition-adapter"
    write_response_adapter(
        adapter,
        {
            name: json.dumps(
                json.loads((case / name).read_text())
                | {"evidence_basis": "implemented"}
            )
            for name in ("composition-blueprint.json", "deployment-schema.json")
        },
        "succeeded",
    )
    write_fixture_composition_descriptor(
        repository, entrypoint=adapter, mode="implemented"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Select implemented composition adapter")
    commit_sha = git(repository, "rev-parse", "HEAD")

    conclusion, result = execute_chain(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
    )

    assert conclusion.returncode == 0, conclusion.stderr
    assert [attempt["domain_outcome"] for attempt in result["attempts"][:3]] == [
        "succeeded",
        "succeeded",
        "not_evaluated",
    ]
    assert [attempt["reason"]["code"] for attempt in result["attempts"][1:]] == [
        "sdi.image-build.recorded",
        "sdi.cv.not-evaluated",
        "sdi.dependency.implemented-evidence",
    ]
    images = json.loads(
        (
            tmp_path / "run/transfers/image_build/outputs/image-build-result.json"
        ).read_text()
    )["images"]
    assert {(item["status"], item["reason"]) for item in images} == {
        ("failed", "unresolved-placement")
    }
    services = json.loads(
        (tmp_path / "run/transfers/cv/outputs/validation-evidence.json").read_text()
    )["services"]
    assert {(item["participation"], item["reason"]) for item in services} == {
        ("not_evaluated", "image-unavailable")
    }


def _described_fixture_services(tmp_path: Path) -> Path:
    repository = tmp_path / "described-service-repository"
    shutil.copytree(FIXTURE_SERVICE_REPOSITORY, repository)
    for service_id in SERVICE_IDS:
        front_matter = description(
            service_id, {f"/{service_id}/state": "std_msgs/msg/String"}
        )
        (repository / service_id).mkdir()
        (repository / service_id / "SDI.md").write_text(
            f"---\n{json.dumps(front_matter)}\n---\n\n# {service_id}\n"
        )
    return repository


IMAGES = {
    "fixture-localization": {
        "service_id": "fixture-localization",
        "status": "built",
        "repository": "sdi/fixture-localization",
        "digest": DIGEST,
        "platforms": [{"architecture": "arm64", "digest": DIGEST}],
    },
    "fixture-navigation": {
        "service_id": "fixture-navigation",
        "status": "failed",
        "reason": "no-recipe",
        "detail": "the artifact names no recipe in the service folder",
    },
    "fixture-mission-control": {
        "service_id": "fixture-mission-control",
        "status": "failed",
        "reason": "unresolved-placement",
        "detail": "composition left the placement unresolved",
    },
}


def _implemented_image_build(
    tmp_path: Path,
    images: dict[str, dict[str, Any]] | None,
    service_repository: Path = FIXTURE_SERVICE_REPOSITORY,
) -> dict[str, Any]:
    repository = implemented_upstream(tmp_path, images, service_repository)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Select implemented composition and image-build")
    commit_sha = git(repository, "rev-parse", "HEAD")

    composed = tmp_path / "transfers" / "composition"
    executed = _execute_stage(
        repository,
        commit_sha,
        "composition",
        composed,
        COMPOSITION_CONFIG_ARGUMENTS,
    )
    assert executed.returncode == 0, executed.stderr
    built = tmp_path / "transfers" / "image_build"
    executed = _execute_stage(
        repository,
        commit_sha,
        "image_build",
        built,
        ("--prior-attempt-root", str(composed)),
    )
    assert executed.returncode == 0, executed.stderr
    return json.loads((built / "accepted-attempt.json").read_text())


def test_a_buildable_placement_needs_an_answering_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_repository = tmp_path / "service-repository"
    folder = service_repository / "fixture-localization"
    folder.mkdir(parents=True)
    front_matter = {
        "schema_version": "sdi.service-description/v1",
        "service_id": "fixture-localization",
        "title": "Fixture localization",
        "provenance": {"basis": "placeholder", "note": "An image-build test."},
        "artifacts": {
            "debs": {
                "architectures": ["arm64"],
                "route": {"kind": "image", "reference": "example/test"},
                "dockerfile": "Dockerfile",
            }
        },
    }
    (folder / "SDI.md").write_text(
        f"---\n{json.dumps(front_matter)}\n---\n\n# Fixture localization\n"
    )
    (folder / "Dockerfile").write_text("FROM scratch\n")
    # The adapter's builder address is fixed; a stand-in buildctl refuses it.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "buildctl").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "buildctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    attempt = _implemented_image_build(tmp_path, None, service_repository)

    assert attempt["execution_conclusion"] == "failed"
    assert attempt["domain_outcome"] == "not_evaluated"
    assert attempt["reason"]["code"] == "sdi.image-build.builder-unreachable"


def test_implemented_image_build_records_each_placement(tmp_path: Path) -> None:
    attempt = _implemented_image_build(tmp_path, IMAGES)

    assert attempt["adapter_response_accepted"] is True, attempt["reason"]
    assert attempt["domain_outcome"] == "succeeded"


@pytest.mark.parametrize(
    "record",
    [
        IMAGES["fixture-localization"]
        | {"platforms": [{"architecture": "amd64", "digest": DIGEST}]},
        IMAGES["fixture-localization"]
        | {"service_id": "fixture-mission-control"}
        | {"repository": "sdi/fixture-mission-control"},
        IMAGES["fixture-navigation"] | {"service_id": "fixture-mission-control"},
        IMAGES["fixture-mission-control"] | {"service_id": "fixture-navigation"},
    ],
    ids=[
        "built-without-the-host-architecture",
        "built-for-an-unresolved-placement",
        "unresolved-placement-with-another-reason",
        "resolved-placement-failed-as-unresolved",
    ],
)
def test_implemented_image_build_records_must_match_their_placements(
    tmp_path: Path, record: dict[str, Any]
) -> None:
    attempt = _implemented_image_build(
        tmp_path, IMAGES | {record["service_id"]: record}
    )

    assert attempt["adapter_response_accepted"] is False
    assert attempt["reason"]["code"] == "sdi.adapter.candidate-rejected"


def _commit_and_execute(
    tmp_path: Path, repository: Path
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Select scripted Stage adapters")
    return execute_chain(
        repository,
        git(repository, "rev-parse", "HEAD"),
        SUCCESS_REQUEST,
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
    )


def _implemented_cv_chain(
    tmp_path: Path,
    domain_outcome: str,
    evidence: dict[str, Any] | None,
    attachment: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    repository = implemented_upstream(tmp_path, CV_IMAGES, placements=CV_PLACEMENTS)
    select_evidence_adapter(
        repository,
        "cv",
        domain_outcome,
        evidence,
        CV_REASONS[domain_outcome],
        attachment,
    )
    return _commit_and_execute(tmp_path, repository)


@pytest.mark.parametrize(
    ("evidence", "run_conclusion"),
    [(SUCCEEDED, 0), (FAILED, 1), (NOT_EVALUATED, 0)],
    ids=["succeeded-green", "failed-red", "not-evaluated-green"],
)
def test_cv_verdict_sets_the_run_colour(
    tmp_path: Path, evidence: dict[str, Any], run_conclusion: int
) -> None:
    conclusion, result = _implemented_cv_chain(
        tmp_path, evidence["domain_outcome"], evidence
    )

    cv = result["attempts"][2]
    assert cv["adapter_response_accepted"] is True, cv.get("reason")
    assert cv["domain_outcome"] == evidence["domain_outcome"]
    assert [
        artifact["slot"]
        for artifact in result["artifacts"]
        if artifact["stage"] == "cv"
    ] == ["validation_evidence"]
    assert conclusion.returncode == run_conclusion, conclusion.stderr


# Not a playable video: the runtime and the bundle never parse an attachment.
RECORDING = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4


@pytest.mark.parametrize(
    ("evidence", "run_conclusion"),
    [(SUCCEEDED, 0), (FAILED, 1)],
    ids=["succeeded-green", "failed-red"],
)
def test_cv_recording_reaches_the_bundle_unparsed(
    tmp_path: Path, evidence: dict[str, Any], run_conclusion: int
) -> None:
    conclusion, result = _implemented_cv_chain(
        tmp_path, evidence["domain_outcome"], evidence, RECORDING
    )

    assert conclusion.returncode == run_conclusion, conclusion.stderr
    cv = result["attempts"][2]
    assert cv["adapter_response_accepted"] is True, cv.get("reason")
    (recording,) = [
        artifact
        for artifact in result["artifacts"]
        if artifact["stage"] == "cv" and artifact["role"] == "stage_attachment"
    ]
    assert recording["slot"] == "sim_recording"
    assert recording["path"] == "stages/cv/sim-recording.mp4"
    assert recording["media_type"] == "video/mp4"
    assert recording["byte_size"] == len(RECORDING)
    bundle_root = tmp_path / "run" / "bundle"
    assert (bundle_root / recording["path"]).read_bytes() == RECORDING
    validated = subprocess.run(
        ["sdi-integration", "validate-bundle", "--bundle-root", str(bundle_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


def test_cv_recording_without_evidence_is_rejected(tmp_path: Path) -> None:
    conclusion, result = _implemented_cv_chain(tmp_path, "succeeded", None, RECORDING)

    cv = result["attempts"][2]
    assert cv["adapter_response_accepted"] is False
    assert cv.get("reason", {}).get("code") == "sdi.adapter.output-missing", cv
    assert all(
        artifact["stage"] != "cv" or artifact["role"] == "diagnostic"
        for artifact in result["artifacts"]
    )
    assert conclusion.returncode == 1


def test_cv_implemented_execution_must_publish_its_evidence(tmp_path: Path) -> None:
    conclusion, result = _implemented_cv_chain(tmp_path, "succeeded", None)

    cv = result["attempts"][2]
    assert cv.get("reason", {}).get("code") == "sdi.adapter.output-missing", cv
    assert cv["adapter_response_accepted"] is False
    assert conclusion.returncode == 1


def _failed_evidence(**changes: object) -> dict[str, object]:
    return json.loads(json.dumps(FAILED)) | changes


@pytest.mark.parametrize(
    "evidence",
    [
        _failed_evidence(domain_outcome="succeeded", services=SUCCEEDED["services"]),
        _failed_evidence(services=FAILED["services"][:2]),
        _failed_evidence(blueprint_id="s-04-tc-03-c-05-other"),
        _failed_evidence(deployment_schema_id="s-04-tc-03-c-05-other"),
        _failed_evidence(image_build_result_id="s-04-tc-03-c-05-other"),
        _failed_evidence(
            mission=FAILED["mission"] | {"steps": FAILED["mission"]["steps"][::-1]}
        ),
        _failed_evidence(
            services=[
                FAILED["services"][0] | {"image_digest": DIGEST},
                *FAILED["services"][1:],
            ]
        ),
    ],
    ids=[
        "other-outcome",
        "other-services",
        "other-blueprint",
        "other-deployment-schema",
        "other-image-build-result",
        "steps-out-of-requirement-order",
        "digest-other-than-amd64",
    ],
)
def test_cv_check_rejects_evidence_contradicting_a_failed_attempt(
    tmp_path: Path, evidence: dict[str, Any]
) -> None:
    conclusion, result = _implemented_cv_chain(tmp_path, "failed", evidence)

    cv = result["attempts"][2]
    assert cv["adapter_response_accepted"] is False
    assert cv["reason"]["code"] == "sdi.adapter.candidate-rejected"
    assert conclusion.returncode == 1


def _implemented_cd_chain(
    tmp_path: Path,
    domain_outcome: str,
    evidence: dict[str, Any] | None,
    cv_evidence: dict[str, Any] = SUCCEEDED,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    repository = implemented_upstream(tmp_path, CD_IMAGES, placements=CV_PLACEMENTS)
    select_evidence_adapter(
        repository,
        "cv",
        cv_evidence["domain_outcome"],
        cv_evidence,
        CV_REASONS[cv_evidence["domain_outcome"]],
    )
    select_evidence_adapter(
        repository,
        "cd",
        domain_outcome,
        evidence,
        CD_REASONS[domain_outcome],
    )
    return _commit_and_execute(tmp_path, repository)


@pytest.mark.parametrize(
    ("evidence", "run_conclusion"),
    [(cd.SUCCEEDED, 0), (cd.FAILED, RED_RUN_EXIT), (cd.NOT_EVALUATED, 0)],
    ids=["succeeded-green", "failed-red", "not-evaluated-green"],
)
def test_cd_outcome_sets_the_run_colour(
    tmp_path: Path, evidence: dict[str, Any], run_conclusion: int
) -> None:
    conclusion, result = _implemented_cd_chain(
        tmp_path, evidence["domain_outcome"], evidence
    )

    attempt = result["attempts"][3]
    assert attempt["adapter_response_accepted"] is True, attempt.get("reason")
    assert attempt["domain_outcome"] == evidence["domain_outcome"]
    assert [
        (artifact["slot"], artifact["path"])
        for artifact in result["artifacts"]
        if artifact["stage"] == "cd"
    ] == [("deployment_result", "stages/cd/deployment-result.json")]
    assert conclusion.returncode == run_conclusion, conclusion.stderr


def test_cd_implemented_execution_must_publish_its_evidence(tmp_path: Path) -> None:
    conclusion, result = _implemented_cd_chain(tmp_path, "succeeded", None)

    attempt = result["attempts"][3]
    assert attempt.get("reason", {}).get("code") == "sdi.adapter.output-missing"
    assert attempt["adapter_response_accepted"] is False
    assert conclusion.returncode == RED_RUN_EXIT


def test_cd_fixture_is_accepted_without_evidence(tmp_path: Path) -> None:
    repository, _ = jenkins_repository(tmp_path, fixture_image_build=True)
    select_evidence_adapter(
        repository,
        "cv",
        "not_evaluated",
        None,
        {
            "category": "fixture",
            "code": "sdi.fixture.cv-executed",
            "summary": "A scripted CV Fixture that lets the CD Fixture run.",
        },
    )
    descriptor = repository / STAGES["cv"].jenkins_descriptor
    descriptor.write_text(
        descriptor.read_text().replace(
            "implementation_mode: implemented", "implementation_mode: fixture"
        )
    )

    _, result = _commit_and_execute(tmp_path, repository)

    attempt = result["attempts"][3]
    assert attempt["implementation_mode"] == "fixture"
    assert attempt["adapter_response_accepted"] is True, attempt.get("reason")
    assert attempt["domain_outcome"] == "not_evaluated"
    assert attempt["reason"]["code"] == "sdi.fixture.cd-executed"
    assert [
        artifact["path"]
        for artifact in result["artifacts"]
        if artifact["stage"] == "cd"
    ] == ["stages/cd/diagnostic.txt"]


def _changed(document: dict[str, Any], **changes: object) -> dict[str, Any]:
    return json.loads(json.dumps(document)) | changes


def _with_placement(
    document: dict[str, Any], index: int, **changes: object
) -> dict[str, Any]:
    placements = json.loads(json.dumps(document["placements"]))
    placements[index] |= changes
    return _changed(document, placements=placements)


@pytest.mark.parametrize(
    ("domain_outcome", "evidence"),
    [
        ("failed", cd.SUCCEEDED),
        ("failed", _changed(cd.FAILED, placements=cd.FAILED["placements"][:2])),
        ("failed", _with_placement(cd.FAILED, 1, host="orin", node="orin")),
        ("failed", _changed(cd.FAILED, blueprint_id="s-04-tc-03-c-05-other")),
        (
            "failed",
            _changed(cd.FAILED, deployment_schema_id="s-04-tc-03-c-05-other"),
        ),
        (
            "failed",
            _changed(cd.FAILED, image_build_result_id="s-04-tc-03-c-05-other"),
        ),
        (
            "failed",
            _with_placement(
                cd.FAILED, 0, image=cd.image("fixture-localization", AMD64_DIGEST)
            ),
        ),
        (
            "failed",
            _with_placement(
                cd.FAILED, 0, image_id=cd.image("fixture-localization", AMD64_DIGEST)
            ),
        ),
        (
            "failed",
            _with_placement(
                cd.FAILED, 1, image=cd.image("fixture-navigation", cd.INDEX_DIGEST)
            ),
        ),
        (
            "not_evaluated",
            _with_placement(cd.NOT_EVALUATED, 2, host="waffle"),
        ),
        ("not_evaluated", _changed(cd.NOT_EVALUATED, blueprint_id="other")),
        ("succeeded", _changed(cd.SUCCEEDED, image_build_result_id="other")),
    ],
    ids=[
        "other-outcome",
        "fewer-placements",
        "other-host",
        "other-blueprint",
        "other-deployment-schema",
        "other-image-build-result",
        "deployed-image-of-another-architecture",
        "deployed-image-id-of-another-architecture",
        "failed-image-pinned-to-the-index",
        "not-evaluated-other-host",
        "not-evaluated-other-blueprint",
        "succeeded-other-image-build-result",
    ],
)
def test_cd_check_rejects_a_result_contradicting_its_inputs(
    tmp_path: Path, domain_outcome: str, evidence: dict[str, Any]
) -> None:
    conclusion, result = _implemented_cd_chain(tmp_path, domain_outcome, evidence)

    attempt = result["attempts"][3]
    assert attempt["adapter_response_accepted"] is False
    assert attempt["reason"]["code"] == "sdi.adapter.candidate-rejected"
    assert conclusion.returncode == RED_RUN_EXIT


def _deployment_manifest(
    bundle_root: Path, output: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "deployment-manifest",
            "--bundle-root",
            str(bundle_root),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "evidence", [cd.SUCCEEDED, cd.FAILED], ids=["succeeded", "failed"]
)
def test_cd_manifest_command_writes_the_manifest_the_evidence_records(
    tmp_path: Path, evidence: dict[str, Any]
) -> None:
    _implemented_cd_chain(tmp_path, evidence["domain_outcome"], evidence)
    bundle_root = tmp_path / "run" / "bundle"
    output = tmp_path / "deployment-manifest.yaml"

    written = _deployment_manifest(bundle_root, output)

    assert written.returncode == 0, written.stderr
    recorded = json.loads(
        (bundle_root / "stages/cd/deployment-result.json").read_text()
    )
    assert output.read_bytes() == manifest_yaml(recorded["manifest"])


@pytest.mark.parametrize(
    ("cv_evidence", "evidence", "cd_conclusion"),
    [
        (SUCCEEDED, cd.NOT_EVALUATED, "succeeded"),
        (FAILED, cd.SUCCEEDED, "skipped"),
        (SUCCEEDED, None, "failed"),
    ],
    ids=["not-evaluated", "skipped", "machinery-failed"],
)
def test_cd_manifest_command_writes_nothing_without_a_manifest(
    tmp_path: Path,
    cv_evidence: dict[str, Any],
    evidence: dict[str, Any] | None,
    cd_conclusion: str,
) -> None:
    _, result = _implemented_cd_chain(
        tmp_path,
        "succeeded" if evidence is None else evidence["domain_outcome"],
        evidence,
        cv_evidence,
    )
    attempt = result["attempts"][3]
    assert attempt["execution_conclusion"] == cd_conclusion
    output = tmp_path / "deployment-manifest.yaml"

    written = _deployment_manifest(tmp_path / "run" / "bundle", output)

    assert written.returncode == 0, written.stderr
    assert not output.exists()


def test_cd_manifest_command_reads_only_a_valid_bundle(tmp_path: Path) -> None:
    _implemented_cd_chain(tmp_path, "succeeded", cd.SUCCEEDED)
    bundle_root = tmp_path / "run" / "bundle"
    evidence = bundle_root / "stages/cd/deployment-result.json"
    evidence.write_text(evidence.read_text().replace("waffle", "burger"))
    output = tmp_path / "deployment-manifest.yaml"

    written = _deployment_manifest(bundle_root, output)

    assert written.returncode == 2
    assert "does not match its inventory" in written.stderr
    assert not output.exists()


def test_an_all_implemented_run_summary_has_no_fixture_warning(
    tmp_path: Path,
) -> None:
    _, result = _implemented_cd_chain(tmp_path, "succeeded", cd.SUCCEEDED)
    assert {attempt["implementation_mode"] for attempt in result["attempts"]} == {
        "implemented"
    }

    summary = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            EXECUTION_ID,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(tmp_path / "handoff-receipt.json"),
            "--bundle-root",
            str(tmp_path / "run" / "bundle"),
            "--handoff-outcome",
            "success",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{EXECUTION_ID}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert "- Result: `validated`" in summary.stdout, summary.stderr
    assert "## Evidence limits" in summary.stdout
    assert "KPI evaluation: `not evaluated`" in summary.stdout
    assert "Fixture warning" not in summary.stdout


def test_bundle_validation_rejects_a_fixture_notice_when_no_stage_is_a_fixture(
    tmp_path: Path,
) -> None:
    _implemented_cd_chain(tmp_path, "succeeded", cd.SUCCEEDED)
    bundle_root = tmp_path / "run" / "bundle"
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["fixture_notice"] = FIXTURE_NOTICE
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        ["sdi-integration", "validate-bundle", "--bundle-root", str(bundle_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "fixture_notice" in validated.stderr


def test_preflight_rejects_a_key_the_composition_descriptor_does_not_bind(
    tmp_path: Path,
) -> None:
    repository, _commit_sha = jenkins_repository(tmp_path)
    write_fixture_composition_descriptor(repository, mode="implemented")
    descriptor = repository / STAGES["composition"].jenkins_descriptor
    descriptor.write_text(
        descriptor.read_text().replace(
            "secret_bindings: []", "secret_bindings: [OTHER_KEY]"
        )
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Bind a key the config does not name")

    preflight = run_preflight(
        repository,
        git(repository, "rev-parse", "HEAD"),
        SUCCESS_REQUEST,
        str(int((time.time() - 1) * 1000)),
    )

    assert preflight.returncode == 2
    assert "does not bind" in preflight.stderr


def _commit_integration_change(repository: Path) -> str:
    source = repository / "integration/src/sdi_pipeline_integration/_cli.py"
    _ = source.write_text(f"{source.read_text()}# merged after the image build\n")
    git(repository, "commit", "-am", "Change the baked integration code")
    return git(repository, "rev-parse", "HEAD")


def test_preflight_names_every_stale_agent_before_any_stage_work(
    tmp_path: Path,
) -> None:
    repository, _commit_sha = jenkins_repository(tmp_path)
    built = baked_code_digest(repository)
    commit_sha = _commit_integration_change(repository)

    preflight = run_preflight(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        str(int((time.time() - 1) * 1000)),
        baked={"image-build": built, "cv": "missing"},
    )

    assert preflight.returncode == 4
    assert preflight.stdout == ""
    assert preflight.stderr.startswith("sdi-integration: sdi.agent.stale-image: ")
    assert "agent images image-build, cv were built" in preflight.stderr
    assert f"image-build {built[:12]}, cv missing" in preflight.stderr
    assert "rebuild the agent images" in preflight.stderr


def test_a_stale_integration_agent_judges_no_other_agent(tmp_path: Path) -> None:
    repository, _commit_sha = jenkins_repository(tmp_path)
    built = baked_code_digest(repository)
    commit_sha = _commit_integration_change(repository)

    preflight = run_preflight(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        str(int((time.time() - 1) * 1000)),
        baked={"integration": built, "cv": "missing"},
    )

    assert preflight.returncode == 4
    assert "the integration agent image was built" in preflight.stderr
    assert "the other agents were not checked" in preflight.stderr
    assert "cv" not in preflight.stderr


def test_stale_stage_agent_refuses_its_stage_without_an_attempt(
    tmp_path: Path,
) -> None:
    repository, _commit_sha = jenkins_repository(tmp_path)
    built = baked_code_digest(repository)
    commit_sha = _commit_integration_change(repository)
    attempt = tmp_path / "attempt"

    executed = subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *common_arguments(repository, commit_sha, SUCCESS_REQUEST),
            "--baked-code-digest",
            built,
            *COMPOSITION_CONFIG_ARGUMENTS,
            "--stage",
            "composition",
            "--attempt-root",
            str(attempt),
            "--run-deadline-epoch-millis",
            str(int(time.time() * 1000) + 60_000),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert executed.returncode == 4
    assert "sdi.agent.stale-image: the ci agent image was built" in executed.stderr
    assert not attempt.exists()


def test_malformed_final_bundle_is_rejected_by_public_cli(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path, fixture_image_build=True)
    root = tmp_path / "run"

    conclusion, _result = execute_chain(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        root,
        str(int((time.time() - 1) * 1000)),
    )
    assert conclusion.returncode == RED_RUN_EXIT, conclusion.stderr
    (root / "bundle" / "pipeline-integration-result.json").write_text("{}")

    rejected = subprocess.run(
        [
            "sdi-integration",
            "bundle-conclusion",
            "--bundle-root",
            str(root / "bundle"),
            "--execution-id",
            EXECUTION_ID,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode == 2
    assert "contract validation failed" in rejected.stderr


@pytest.mark.parametrize(
    ("request_path", "expected_conclusion", "first_outcome"),
    [
        ("runs/conformance/composition-negative-domain.yaml", 0, "succeeded"),
        ("runs/conformance/composition-handled-failure.yaml", 1, "failed"),
        ("runs/conformance/composition-malformed-response.yaml", 1, "failed"),
    ],
)
def test_distributed_failures_finalize_typed_skips_and_machinery_conclusion(
    tmp_path: Path,
    request_path: str,
    expected_conclusion: int,
    first_outcome: str,
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)

    conclusion, result = execute_chain(
        repository,
        commit_sha,
        request_path,
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
    )

    assert conclusion.returncode == expected_conclusion, conclusion.stderr
    assert result["attempts"][0]["execution_conclusion"] == first_outcome
    assert all(
        attempt["execution_conclusion"] == "skipped"
        for attempt in result["attempts"][1:]
    )


@pytest.mark.slow
def test_distributed_timeout_records_one_attempt_and_skips(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)

    conclusion, result = execute_chain(
        repository,
        commit_sha,
        "runs/conformance/composition-timeout.yaml",
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
        run_limit_seconds=2,
    )

    assert conclusion.returncode == 1, conclusion.stderr
    assert result["attempts"][0]["execution_conclusion"] == "timed_out"
    assert result["attempts"][0]["reason"]["code"] == "sdi.run.deadline-exceeded"
    assert all(
        attempt["execution_conclusion"] == "skipped"
        for attempt in result["attempts"][1:]
    )


@pytest.mark.slow
def test_trusted_stage_work_limit_overrides_profile_default(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)

    conclusion, result = execute_chain(
        repository,
        commit_sha,
        "runs/conformance/composition-timeout.yaml",
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
        work_limits={"composition": "1"},
    )

    assert conclusion.returncode == 1, conclusion.stderr
    assert result["attempts"][0]["execution_conclusion"] == "timed_out"
    assert result["attempts"][0]["reason"]["code"] == "sdi.stage.deadline-exceeded"


def test_expired_run_deadline_does_not_launch_stage(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    attempt = tmp_path / "attempt"

    completed = subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *common_arguments(repository, commit_sha, SUCCESS_REQUEST),
            *fresh_image(repository),
            *COMPOSITION_CONFIG_ARGUMENTS,
            "--stage",
            "composition",
            "--attempt-root",
            str(attempt),
            "--run-deadline-epoch-millis",
            str(int(time.time() * 1000) - 1),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 3
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not attempt.exists()


def test_run_deadline_before_stage_work_materializes_typed_skips(
    tmp_path: Path,
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    finalized = subprocess.run(
        [
            "sdi-integration",
            "finalize-jenkins-run",
            *common_arguments(repository, commit_sha, SUCCESS_REQUEST),
            "--build-started-at-millis",
            str(int((time.time() - 1) * 1000)),
            "--run-deadline-expired",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert finalized.returncode == 0, finalized.stderr
    result = json.loads(finalized.stdout)
    assert result["attempts"][0]["reason"]["code"] == "sdi.run.deadline-exceeded"
    assert all(
        attempt["execution_conclusion"] == "skipped" for attempt in result["attempts"]
    )
    conclusion = subprocess.run(
        [
            "sdi-integration",
            "bundle-conclusion",
            "--bundle-root",
            str(bundle_root),
            "--execution-id",
            EXECUTION_ID,
        ],
        check=False,
    )
    assert conclusion.returncode == 1


def test_tampered_attempt_transfer_is_rejected(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    root = tmp_path / "run"
    started_millis = str(int((time.time() - 1) * 1000))
    assert (
        run_preflight(
            repository, commit_sha, SUCCESS_REQUEST, started_millis
        ).returncode
        == 0
    )
    attempt = root / "composition"
    executed = subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *common_arguments(repository, commit_sha, SUCCESS_REQUEST),
            *fresh_image(repository),
            *COMPOSITION_CONFIG_ARGUMENTS,
            "--stage",
            "composition",
            "--attempt-root",
            str(attempt),
            "--run-deadline-epoch-millis",
            str(int(time.time() * 1000) + 60_000),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stderr
    (attempt / "outputs/composition-blueprint.json").write_text("{}")

    continuation = subprocess.run(
        [
            "sdi-integration",
            "attempt-allows-continuation",
            "--attempt-root",
            str(attempt),
            "--execution-id",
            EXECUTION_ID,
            "--stage",
            "composition",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert continuation.returncode == 2
    assert "does not match its envelope" in continuation.stderr


def test_external_cancellation_publishes_no_attempt(tmp_path: Path) -> None:
    repository, _commit_sha = jenkins_repository(tmp_path)
    adapter = tmp_path / "cancellable-adapter"
    ready = tmp_path / "ready"
    adapter.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, time\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('command')\n"
        "parser.add_argument('--request')\n"
        "parser.add_argument('--input-root')\n"
        "parser.add_argument('--output-root')\n"
        "parser.parse_args()\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(60)\n"
    )
    adapter.chmod(0o755)
    descriptor = repository / "deployment/jenkins/adapters/composition-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            "entrypoint: sdi-fixture-adapter", f"entrypoint: {adapter}"
        )
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Select cancellable adapter")
    commit_sha = git(repository, "rev-parse", "HEAD")
    attempt = tmp_path / "attempt"
    process = subprocess.Popen(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *common_arguments(repository, commit_sha, SUCCESS_REQUEST),
            *fresh_image(repository),
            *COMPOSITION_CONFIG_ARGUMENTS,
            "--stage",
            "composition",
            "--attempt-root",
            str(attempt),
            "--run-deadline-epoch-millis",
            str(int(time.time() * 1000) + 60_000),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 128 + signal.SIGTERM, stderr
    assert stdout == ""
    assert not attempt.exists()
    assert not any(
        path.name.startswith(".attempt.work-") for path in tmp_path.iterdir()
    )

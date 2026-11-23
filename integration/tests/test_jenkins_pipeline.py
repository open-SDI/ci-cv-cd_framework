"""Black-box checks for the public CLI seams used by Jenkins."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from sdi_pipeline_integration._integration_code import integration_code_digest
from sdi_pipeline_integration._stage_registry import AGENTS, INTEGRATION_AGENT, STAGES
from tests._fixture_inputs import (
    FIXTURE_COMPOSITION_CONFIG,
    FIXTURE_SERVICE_REPOSITORY,
    STAGE_DESCRIPTORS,
    copy_baked_integration_code,
    write_fixture_composition_descriptor,
    write_scripted_adapter,
)

SOURCE_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_ID = "12345678-1234-4234-9234-123456789abc"
SUCCESS_REQUEST = "runs/s-04/s-04-tc-03-c-05-fixture.yaml"
COMPOSITION_CONFIG_ARGUMENTS = ("--composition-config", FIXTURE_COMPOSITION_CONFIG)
COMMITTED_FILES = (
    SUCCESS_REQUEST,
    "requirements/s-04/deliver-book-to-joe.md",
    "profiles/s-04/waffle-native-arm64.yaml",
    "profiles/s-04/waffle-jetson-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    *STAGE_DESCRIPTORS,
    "runs/conformance/composition-negative-domain.yaml",
    "runs/conformance/composition-handled-failure.yaml",
    "runs/conformance/composition-malformed-response.yaml",
    "runs/conformance/composition-timeout.yaml",
    FIXTURE_COMPOSITION_CONFIG,
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(
    tmp_path: Path,
    *,
    fixture_image_build: bool = False,
    service_repository: Path = FIXTURE_SERVICE_REPOSITORY,
) -> tuple[Path, str]:
    """Commit the Stage inputs; a Fixture image-build completes the chain."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test Operator")
    _git(repository, "config", "user.email", "operator@example.test")
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "https://github.com/example/sdi-fixture.git",
    )
    for relative_path in COMMITTED_FILES:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((SOURCE_ROOT / relative_path).read_bytes())
    shutil.copytree(service_repository, repository / "service-repository")
    copy_baked_integration_code(repository)
    write_fixture_composition_descriptor(repository)
    if fixture_image_build:
        adapters = repository / "deployment/jenkins/adapters"
        (adapters / "image-build-v1.yaml").write_bytes(
            (adapters / "image-build-fixture-v1.yaml").read_bytes()
        )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add distributed Fixture inputs")
    return repository, _git(repository, "rev-parse", "HEAD")


def _common(repository: Path, commit_sha: str, request_path: str) -> list[str]:
    return [
        "--repository",
        str(repository),
        "--requested-ref",
        "refs/heads/main",
        "--resolved-commit",
        commit_sha,
        "--run-request-path",
        request_path,
        "--execution-id",
        EXECUTION_ID,
    ]


def _baked_code_digest(repository: Path) -> str:
    """Record the checkout's integration code as a freshly built image would."""
    return integration_code_digest(repository / "integration")


def _fresh_image(repository: Path) -> tuple[str, str]:
    return ("--baked-code-digest", _baked_code_digest(repository))


def _preflight(
    repository: Path,
    commit_sha: str,
    request_path: str,
    started_millis: str,
    *,
    baked: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fresh = _baked_code_digest(repository)
    recorded = dict.fromkeys((agent.node for agent in AGENTS), fresh) | (baked or {})
    return subprocess.run(
        [
            "sdi-integration",
            "preflight-jenkins-run",
            *_common(repository, commit_sha, request_path),
            "--composition-config",
            FIXTURE_COMPOSITION_CONFIG,
            "--handoff-contract-version",
            "sdi.github-jenkins-handoff/v1",
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--build-started-at-millis",
            started_millis,
            "--integration-node",
            INTEGRATION_AGENT.node,
            "--integration-baked-code-digest",
            recorded[INTEGRATION_AGENT.node],
            *(
                argument
                for name, stage in STAGES.items()
                for argument in (
                    "--stage-node",
                    f"{name}={stage.agent.node}",
                    "--stage-baked-code-digest",
                    f"{name}={recorded[stage.agent.node]}",
                )
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _execute_chain(  # noqa: PLR0913, PLR0917
    repository: Path,
    commit_sha: str,
    request_path: str,
    root: Path,
    started_millis: str,
    run_limit_seconds: int = 60,
    work_limits: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run every Stage, each given the supplied inputs it is granted."""
    preflight = _preflight(repository, commit_sha, request_path, started_millis)
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout)["execution_id"] == EXECUTION_ID

    attempts: list[Path] = []
    work_limits = work_limits or {}
    deadline = str(int((time.time() + run_limit_seconds) * 1000))
    for stage in STAGES:
        attempt_root = root / "transfers" / stage
        command = [
            "sdi-integration",
            "execute-jenkins-stage",
            *_common(repository, commit_sha, request_path),
            *_fresh_image(repository),
            *(COMPOSITION_CONFIG_ARGUMENTS if stage == "composition" else ()),
            "--stage",
            stage,
            "--attempt-root",
            str(attempt_root),
            "--run-deadline-epoch-millis",
            deadline,
        ]
        if stage in work_limits:
            command.extend(("--work-limit-seconds", work_limits[stage]))
        for prior in attempts:
            command.extend(("--prior-attempt-root", str(prior)))
        executed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        assert executed.returncode == 0, executed.stderr
        attempts.append(attempt_root)
        continuation = subprocess.run(
            [
                "sdi-integration",
                "attempt-allows-continuation",
                "--attempt-root",
                str(attempt_root),
                "--execution-id",
                EXECUTION_ID,
                "--stage",
                stage,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert continuation.returncode in {0, 1}, continuation.stderr
        if continuation.returncode == 1:
            break

    bundle_root = root / "bundle"
    command = [
        "sdi-integration",
        "finalize-jenkins-run",
        *_common(repository, commit_sha, request_path),
        "--build-started-at-millis",
        started_millis,
        "--bundle-root",
        str(bundle_root),
    ]
    for attempt in attempts:
        command.extend(("--attempt-root", str(attempt)))
    finalized = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    assert finalized.returncode == 0, finalized.stderr
    result = json.loads(finalized.stdout)
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
        capture_output=True,
        text=True,
    )
    return conclusion, result


def test_distributed_fixture_chain_preserves_identity_and_exact_archive(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _repository(tmp_path, fixture_image_build=True)
    started_millis = str(int((time.time() - 1) * 1000))

    conclusion, result = _execute_chain(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        tmp_path / "run",
        started_millis,
    )

    assert conclusion.returncode == 0, conclusion.stderr
    assert result["execution_id"] == EXECUTION_ID
    assert [attempt["correlation"]["stage"] for attempt in result["attempts"]] == [
        *STAGES
    ]
    assert len(result["artifacts"]) == 9
    assert all(
        attempt["execution_conclusion"] == "succeeded" for attempt in result["attempts"]
    )


def test_implemented_image_build_skips_fixture_composition(tmp_path: Path) -> None:
    repository, commit_sha = _repository(tmp_path)

    conclusion, result = _execute_chain(
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
            *_common(repository, commit_sha, SUCCESS_REQUEST),
            *_fresh_image(repository),
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
    repository, commit_sha = _repository(tmp_path)
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
            *_common(repository, commit_sha, SUCCESS_REQUEST),
            *_fresh_image(repository),
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
    repository, commit_sha = _repository(tmp_path)
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


def test_a_stage_without_a_work_limit_gets_its_full_grant(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    recorded = tmp_path / "work-limit"
    adapter = tmp_path / "recording-composition-adapter"
    write_scripted_adapter(
        adapter,
        f"Path({str(recorded)!r}).write_text(str(request['work_limit_seconds']))\n",
    )
    write_fixture_composition_descriptor(
        repository, entrypoint=adapter, mode="implemented"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select a recording composition adapter")

    executed = _execute_composition(
        repository, _git(repository, "rev-parse", "HEAD"), tmp_path / "attempt"
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
    repository, commit_sha = _repository(tmp_path)
    attempt = tmp_path / "attempt"

    executed = _execute_stage(repository, commit_sha, stage, attempt, supplied)

    assert executed.returncode == 2
    assert f"the {stage} Stage is not granted a supplied {label}" in executed.stderr
    assert not attempt.exists()


def test_implemented_composition_runs_the_image_build_adapter(
    tmp_path: Path,
) -> None:
    # Every Fixture placement is unresolved, so nothing reaches the builder.
    repository, _commit_sha = _repository(tmp_path)
    case = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
    blueprint, deployment = (
        json.dumps(
            json.loads((case / name).read_text()) | {"evidence_basis": "implemented"}
        )
        for name in ("composition-blueprint.json", "deployment-schema.json")
    )
    adapter = tmp_path / "implemented-composition-adapter"
    write_scripted_adapter(
        adapter,
        f"""(output / "outputs").mkdir()
(output / "outputs/composition-blueprint.json").write_text({blueprint!r})
(output / "outputs/deployment-schema.json").write_text({deployment!r})
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "succeeded",
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": ["composition_blueprint", "deployment_schema"],
    "diagnostic": {{"present": False, "truncated": False}},
}}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    write_fixture_composition_descriptor(
        repository, entrypoint=adapter, mode="implemented"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select implemented composition adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")

    conclusion, result = _execute_chain(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
    )

    assert conclusion.returncode == 0, conclusion.stderr
    assert [attempt["domain_outcome"] for attempt in result["attempts"][:2]] == [
        "succeeded",
        "succeeded",
    ]
    assert [attempt["reason"]["code"] for attempt in result["attempts"][1:]] == [
        "sdi.image-build.recorded",
        "sdi.dependency.implemented-evidence",
        "sdi.dependency.prerequisite-blocked",
    ]
    images = json.loads(
        (
            tmp_path / "run/transfers/image_build/outputs/image-build-result.json"
        ).read_text()
    )["images"]
    assert {(item["status"], item["reason"]) for item in images} == {
        ("failed", "unresolved-placement")
    }


# Both profile hosts are arm64; mission control stays unresolved yet keeps a host.
PLACEMENTS = [
    {
        "service_id": "fixture-localization",
        "status": "resolved",
        "artifact_id": "debs",
        "host": "orin",
    },
    {
        "service_id": "fixture-navigation",
        "status": "resolved",
        "artifact_id": "debs",
        "host": "waffle",
    },
    {"service_id": "fixture-mission-control", "status": "unresolved", "host": "orin"},
]
DIGEST = f"sha256:{'a' * 64}"
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


def _write_response_adapter(
    path: Path, documents: dict[str, str], domain_outcome: str
) -> None:
    """Write an adapter that publishes fixed documents, naming its inputs."""
    write_scripted_adapter(
        path,
        f"""(output / "outputs").mkdir()
sources = [
    {{"slot": item["slot"], "sha256": item["sha256"]}} for item in request["inputs"]
]
for name, document in {documents!r}.items():
    payload = json.loads(document)
    payload["source_inputs"] = sources
    (output / "outputs" / name).write_text(json.dumps(payload))
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": {domain_outcome!r},
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [item["slot"] for item in request["outputs"]
        if item["role"] == "domain_output"],
    "diagnostic": {{"present": False, "truncated": False}},
}}
(output / "response.json").write_text(json.dumps(response))
""",
    )


def _implemented_image_build(
    tmp_path: Path,
    images: dict[str, dict[str, Any]],
    service_repository: Path = FIXTURE_SERVICE_REPOSITORY,
) -> dict[str, Any]:
    """Run implemented composition, then image-build publishing these records.

    With no records, the committed image-build adapter runs instead.
    """
    repository, _commit_sha = _repository(
        tmp_path, service_repository=service_repository
    )
    case = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
    blueprint = json.loads((case / "composition-blueprint.json").read_text())
    deployment = json.loads((case / "deployment-schema.json").read_text())
    composition = tmp_path / "implemented-composition-adapter"
    _write_response_adapter(
        composition,
        {
            "composition-blueprint.json": json.dumps(
                blueprint | {"evidence_basis": "implemented"}
            ),
            "deployment-schema.json": json.dumps(
                deployment | {"evidence_basis": "implemented", "placements": PLACEMENTS}
            ),
        },
        "succeeded",
    )
    write_fixture_composition_descriptor(
        repository, entrypoint=composition, mode="implemented"
    )
    image_build = tmp_path / "implemented-image-build-adapter"
    result = {
        "schema_version": "sdi.image-build-result/v1",
        "evidence_basis": "implemented",
        "image_build_result_id": "s-04-tc-03-c-05-image-build",
        "blueprint_id": blueprint["blueprint_id"],
        "deployment_schema_id": deployment["deployment_schema_id"],
        "scenario_id": "S-04",
        "testcase_id": "TC-03",
        "combination_id": "C-05",
        "profile_id": "waffle-jetson-arm64",
        "source_inputs": [],
        "registry": "registry:5000",
        "images": list(images.values()),
    }
    _write_response_adapter(
        image_build, {"image-build-result.json": json.dumps(result)}, "succeeded"
    )
    descriptor = repository / STAGES["image_build"].jenkins_descriptor
    committed = descriptor.read_text()
    scripted = committed.replace(
        "entrypoint: sdi-image-build-adapter", f"entrypoint: {image_build}"
    )
    assert scripted != committed
    if images:
        descriptor.write_text(scripted)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select implemented composition and image-build")
    commit_sha = _git(repository, "rev-parse", "HEAD")

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

    attempt = _implemented_image_build(tmp_path, {}, service_repository)

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


def test_preflight_rejects_a_key_the_composition_descriptor_does_not_bind(
    tmp_path: Path,
) -> None:
    repository, _commit_sha = _repository(tmp_path)
    write_fixture_composition_descriptor(repository, mode="implemented")
    descriptor = repository / STAGES["composition"].jenkins_descriptor
    descriptor.write_text(
        descriptor.read_text().replace(
            "secret_bindings: []", "secret_bindings: [OTHER_KEY]"
        )
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Bind a key the config does not name")

    preflight = _preflight(
        repository,
        _git(repository, "rev-parse", "HEAD"),
        SUCCESS_REQUEST,
        str(int((time.time() - 1) * 1000)),
    )

    assert preflight.returncode == 2
    assert "does not bind" in preflight.stderr


def _commit_integration_change(repository: Path) -> str:
    source = repository / "integration/src/sdi_pipeline_integration/_cli.py"
    _ = source.write_text(f"{source.read_text()}# merged after the image build\n")
    _git(repository, "commit", "-am", "Change the baked integration code")
    return _git(repository, "rev-parse", "HEAD")


def test_preflight_names_every_stale_agent_before_any_stage_work(
    tmp_path: Path,
) -> None:
    repository, _commit_sha = _repository(tmp_path)
    built = _baked_code_digest(repository)
    commit_sha = _commit_integration_change(repository)

    preflight = _preflight(
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
    repository, _commit_sha = _repository(tmp_path)
    built = _baked_code_digest(repository)
    commit_sha = _commit_integration_change(repository)

    preflight = _preflight(
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
    repository, _commit_sha = _repository(tmp_path)
    built = _baked_code_digest(repository)
    commit_sha = _commit_integration_change(repository)
    attempt = tmp_path / "attempt"

    executed = subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *_common(repository, commit_sha, SUCCESS_REQUEST),
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
    repository, commit_sha = _repository(tmp_path, fixture_image_build=True)
    root = tmp_path / "run"

    conclusion, _result = _execute_chain(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        root,
        str(int((time.time() - 1) * 1000)),
    )
    assert conclusion.returncode == 0, conclusion.stderr
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
    repository, commit_sha = _repository(tmp_path)

    conclusion, result = _execute_chain(
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
    repository, commit_sha = _repository(tmp_path)

    conclusion, result = _execute_chain(
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
    repository, commit_sha = _repository(tmp_path)

    conclusion, result = _execute_chain(
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
    repository, commit_sha = _repository(tmp_path)
    attempt = tmp_path / "attempt"

    completed = subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *_common(repository, commit_sha, SUCCESS_REQUEST),
            *_fresh_image(repository),
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
    repository, commit_sha = _repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    finalized = subprocess.run(
        [
            "sdi-integration",
            "finalize-jenkins-run",
            *_common(repository, commit_sha, SUCCESS_REQUEST),
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
    repository, commit_sha = _repository(tmp_path)
    root = tmp_path / "run"
    started_millis = str(int((time.time() - 1) * 1000))
    assert (
        _preflight(repository, commit_sha, SUCCESS_REQUEST, started_millis).returncode
        == 0
    )
    attempt = root / "composition"
    executed = subprocess.run(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *_common(repository, commit_sha, SUCCESS_REQUEST),
            *_fresh_image(repository),
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
    repository, _commit_sha = _repository(tmp_path)
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
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select cancellable adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    attempt = tmp_path / "attempt"
    process = subprocess.Popen(
        [
            "sdi-integration",
            "execute-jenkins-stage",
            *_common(repository, commit_sha, SUCCESS_REQUEST),
            *_fresh_image(repository),
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

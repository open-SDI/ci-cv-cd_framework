"""Black-box checks for the language-neutral Stage-adapter process seam."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._git_input import GitRepository
from sdi_pipeline_integration._run_input import identify_committed_run
from sdi_pipeline_integration._stage_registry import STAGES
from sdi_pipeline_integration._stage_runtime import SuppliedInputs, stage_inputs
from tests._fixture_inputs import (
    FIXTURE_COMPOSITION_CONFIG,
    FIXTURE_SERVICE_REPOSITORY,
    STAGE_DESCRIPTORS,
    SUPPLIED_INPUT_ARGUMENTS,
    write_fixture_composition_descriptor,
    write_scripted_adapter,
)

if TYPE_CHECKING:
    from sdi_pipeline_integration._stage_registry import StageName

SOURCE_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_FILES = (
    "runs/s-04/s-04-tc-03-c-05-fixture.yaml",
    "requirements/s-04/deliver-book-to-joe.md",
    "profiles/s-04/waffle-jetson-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    *STAGE_DESCRIPTORS,
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


def _commit_fixture_repository(tmp_path: Path) -> tuple[Path, str]:
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
    write_fixture_composition_descriptor(repository)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add composition Fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (
            "composition",
            [
                "run_request",
                "requirements_specification",
                "target_profile",
                "service_repository",
                "composition_settings",
            ],
        ),
        ("image_build", ["target_profile", "service_repository"]),
        ("cv", ["requirements_specification", "target_profile", "service_repository"]),
        ("cd", ["target_profile"]),
    ],
)
def test_each_stage_receives_only_its_granted_run_inputs(
    tmp_path: Path, stage: StageName, expected: list[str]
) -> None:
    repository_path, commit_sha = _commit_fixture_repository(tmp_path)
    identified = identify_committed_run(
        repository_path=repository_path,
        requested_ref="refs/heads/main",
        resolved_commit=commit_sha,
        run_request_path=COMMITTED_FILES[0],
    )
    supplied = (
        SuppliedInputs(
            service_repository=FIXTURE_SERVICE_REPOSITORY,
            composition_config=FIXTURE_COMPOSITION_CONFIG,
        )
        if stage == "composition"
        else SuppliedInputs(service_repository=FIXTURE_SERVICE_REPOSITORY)
        if stage in {"image_build", "cv"}
        else SuppliedInputs()
    )

    inputs = stage_inputs(
        GitRepository(repository_path, commit_sha),
        identified,
        supplied,
        stage=stage,
    )

    assert list(inputs) == expected


def _execute(
    repository: Path,
    commit_sha: str,
    attempt_root: Path,
    supplied: tuple[str, ...] = SUPPLIED_INPUT_ARGUMENTS,
    descriptor: str = "deployment/jenkins/adapters/composition-v1.yaml",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "execute-stage",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            "runs/s-04/s-04-tc-03-c-05-fixture.yaml",
            "--descriptor-path",
            descriptor,
            "--attempt-root",
            str(attempt_root),
            *supplied,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_executes_the_committed_composition_fixture_deterministically(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    first_root = tmp_path / "accepted-first"
    second_root = tmp_path / "accepted-second"

    first = _execute(repository, commit_sha, first_root)
    second = _execute(repository, commit_sha, second_root)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    first_envelope = json.loads(first.stdout)
    second_envelope = json.loads(second.stdout)
    assert (
        first_envelope["correlation"]["execution_id"]
        != second_envelope["correlation"]["execution_id"]
    )
    assert first_envelope["lifecycle_state"] == "completed"
    assert first_envelope["execution_conclusion"] == "succeeded"
    assert first_envelope["implementation_mode"] == "fixture"
    assert first_envelope["domain_outcome"] == "not_evaluated"
    assert first_envelope["process"]["exit_code"] == 0
    assert [item["slot"] for item in first_envelope["accepted_files"]] == [
        "composition_blueprint",
        "deployment_schema",
        "diagnostic",
    ]

    for relative_path in (
        "outputs/composition-blueprint.json",
        "outputs/deployment-schema.json",
        "diagnostic.txt",
    ):
        assert (first_root / relative_path).read_bytes() == (
            second_root / relative_path
        ).read_bytes()
    assert json.loads((first_root / "accepted-attempt.json").read_text()) == (
        first_envelope
    )


def test_composition_requires_a_supplied_service_repository(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    attempt_root = tmp_path / "accepted"

    completed = _execute(
        repository,
        commit_sha,
        attempt_root,
        ("--composition-config", FIXTURE_COMPOSITION_CONFIG),
    )

    assert completed.returncode == 2
    assert "composition Stage requires a supplied service repository" in (
        completed.stderr
    )
    assert not attempt_root.exists()


def test_a_stage_not_granted_the_service_repository_rejects_it(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    attempt_root = tmp_path / "accepted"

    completed = _execute(
        repository,
        commit_sha,
        attempt_root,
        ("--service-repository", str(FIXTURE_SERVICE_REPOSITORY)),
        descriptor=STAGES["cd"].jenkins_descriptor,
    )

    assert completed.returncode == 2
    assert "the cd Stage is not granted a supplied service repository" in (
        completed.stderr
    )
    assert not attempt_root.exists()


def test_records_an_undeclared_candidate_without_salvaging_files(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "nonconforming-adapter"
    write_scripted_adapter(
        adapter,
        """(output / "undeclared.txt").write_text("must not be accepted")
response = {
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "not_evaluated",
    "reason": {
        "category": "fixture",
        "code": "sdi.fixture.nonconforming",
        "summary": "A deliberately invalid conformance candidate.",
    },
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [],
    "diagnostic": {"present": False, "truncated": False},
}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    write_fixture_composition_descriptor(repository, entrypoint=adapter)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select conformance adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    attempt_root = tmp_path / "must-not-exist"

    completed = _execute(repository, commit_sha, attempt_root)

    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["execution_conclusion"] == "failed"
    assert envelope["adapter_response_accepted"] is False
    assert envelope["reason"]["code"] == "sdi.adapter.undeclared-output"
    assert envelope["accepted_files"] == []
    assert {path.name for path in attempt_root.iterdir()} == {"accepted-attempt.json"}
    assert not any(
        path.read_bytes() == b"must not be accepted"
        for path in tmp_path.rglob("undeclared.txt")
        if path.is_file()
    )


def test_classifies_off_contract_output_by_its_contract_failure(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "off-contract-adapter"
    # Pydantic quotes the offending input, so the failure text names a device.
    write_scripted_adapter(
        adapter,
        """(output / "outputs").mkdir()
(output / "outputs/composition-evidence.json").write_text(
    json.dumps({"devices": ["camera"]})
)
response = {
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "failed",
    "reason": {
        "category": "domain",
        "code": "sdi.fixture.off-contract",
        "summary": "The candidate evidence is outside its contract.",
    },
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": ["composition_evidence"],
    "diagnostic": {"present": False, "truncated": False},
}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    write_fixture_composition_descriptor(repository, entrypoint=adapter)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select off-contract adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")

    completed = _execute(repository, commit_sha, tmp_path / "attempt")

    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["adapter_response_accepted"] is False
    assert envelope["reason"]["code"] == "sdi.adapter.schema-mismatch"


def test_records_a_replaced_candidate_root_without_accepting_its_response(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "root-replacing-adapter"
    write_scripted_adapter(
        adapter,
        """output.rmdir()
replacement = output.parent / "replacement"
replacement.mkdir()
output.symlink_to(replacement, target_is_directory=True)
response = {
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "not_evaluated",
    "reason": {
        "category": "fixture",
        "code": "sdi.fixture.nonconforming",
        "summary": "The candidate root was replaced.",
    },
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [],
    "diagnostic": {"present": False, "truncated": False},
}
(replacement / "response.json").write_text(json.dumps(response))
""",
    )
    write_fixture_composition_descriptor(repository, entrypoint=adapter)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select root-replacing adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    attempt_root = tmp_path / "must-not-exist"

    completed = _execute(repository, commit_sha, attempt_root)

    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["execution_conclusion"] == "failed"
    assert envelope["adapter_response_accepted"] is False
    assert envelope["reason"]["code"] == "sdi.adapter.response-race"
    assert envelope["accepted_files"] == []
    assert {path.name for path in attempt_root.iterdir()} == {"accepted-attempt.json"}


def test_rejects_a_hand_edited_profile_before_invocation(tmp_path: Path) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    profile = repository / "integration/stage-profiles/composition-v1.yaml"
    profile.write_text(
        profile.read_text().replace("max_bytes: 262144", "max_bytes: 262145"),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Widen an output grant by hand")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    attempt_root = tmp_path / "must-not-exist"

    completed = _execute(repository, commit_sha, attempt_root)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "sdi-integration: integration/stage-profiles/composition-v1.yaml does not "
        "match the composition profile rendered from the registry; run "
        "sdi-integration stage-profiles --write\n"
    )
    assert not attempt_root.exists()

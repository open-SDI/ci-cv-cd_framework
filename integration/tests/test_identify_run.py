"""Black-box checks for committed run identification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

RUN_REQUEST = """\
schema_version: sdi.pipeline-integration-run-request/v1
scenario_id: S-04
testcase_id: TC-03
requirements_specification: requirements/delivery.yaml
combination:
  combination_id: C-01
  target_profile: profiles/waffle-native-arm64.yaml
"""

REQUIREMENTS_SPECIFICATION = """\
schema_version: sdi.mobility-requirements-specification/v1
document_version: 1
purpose: Define a deterministic delivery-navigation Fixture mission.
scope: Pipeline-interface validation only.
system_context:
  description: A TurtleBot delivers a book to a named drop-off point.
  parameters:
    - parameter_id: arrival-radius
      description: Maximum distance from the destination for arrival.
      unit: m
      value:
        value: 0.25
        basis: placeholder
        note: Interface-only value; no mobility capability is claimed.
stakeholders:
  - stakeholder_id: delivery-operator
    name: Delivery operator
definitions:
  - term: arrival
    definition: Reaching the configured destination radius.
assumptions:
  - A map is available before navigation starts.
dependencies:
  - A localization service provides the current pose.
external_interfaces:
  - interface_id: navigation-goal
    description: Receives the selected destination.
stakeholder_needs:
  - need_id: complete-delivery
    stakeholder_id: delivery-operator
    statement: The delivery reaches the selected destination.
requirements:
  - requirement_id: reach-destination
    type: functional
    title: Reach the selected destination
    normative_statement: The mobility target shall reach the selected destination.
    priority: must
    rationale: Arrival is necessary to complete delivery.
    source: S-04 Fixture definition.
    traces_to:
      - complete-delivery
    depends_on: []
    verification:
      method: test
      acceptance_criteria:
        - Final distance is within the arrival-radius parameter.
"""

TARGET_PROFILE = """\
schema_version: sdi.target-execution-profile/v1
profile_id: waffle-native-arm64
target:
  target_id: turtlebot3-waffle
  kind: sdv
platform:
  platform_id: waffle-native
  kind: native
  architecture: arm64
"""


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def committed_inputs(tmp_path: Path) -> tuple[Path, str]:
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

    inputs = {
        "runs/request.yaml": RUN_REQUEST,
        "requirements/delivery.yaml": REQUIREMENTS_SPECIFICATION,
        "profiles/waffle-native-arm64.yaml": TARGET_PROFILE,
    }
    for relative_path, content in inputs.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add inputs")
    return repository, _git(repository, "rev-parse", "HEAD")


def _identify(repository: Path, commit_sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "identify-run",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            "runs/request.yaml",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _replace_and_commit(repository: Path, relative_path: str, content: str) -> str:
    (repository / relative_path).write_text(content, encoding="utf-8")
    _git(repository, "add", relative_path)
    _git(repository, "commit", "-m", f"Change {relative_path}")
    return _git(repository, "rev-parse", "HEAD")


def _replace_bytes_and_commit(
    repository: Path, relative_path: str, content: bytes
) -> str:
    (repository / relative_path).write_bytes(content)
    _git(repository, "add", relative_path)
    _git(repository, "commit", "-m", f"Change {relative_path}")
    return _git(repository, "rev-parse", "HEAD")


def _assert_rejected_without_identity(
    completed: subprocess.CompletedProcess[str], expected_error: str
) -> None:
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert expected_error.lower() in completed.stderr.lower()
    assert re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-", completed.stderr) is None


def test_identifies_a_valid_committed_run(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, commit_sha = committed_inputs

    completed = _identify(repository, commit_sha)

    assert completed.returncode == 0, completed.stderr
    identified = json.loads(completed.stdout)
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        identified["execution_id"],
    )
    assert identified["repository"] == "github.com/example/sdi-fixture"
    assert identified["requested_ref"] == "refs/heads/main"
    assert identified["resolved_commit_sha"] == commit_sha
    assert identified["scenario_id"] == "S-04"
    assert identified["testcase_id"] == "TC-03"
    assert identified["combination_id"] == "C-01"
    assert identified["profile_id"] == "waffle-native-arm64"
    assert [item["path"] for item in identified["inputs"]] == [
        "runs/request.yaml",
        "requirements/delivery.yaml",
        "profiles/waffle-native-arm64.yaml",
    ]
    assert [item["schema_version"] for item in identified["inputs"]] == [
        "sdi.pipeline-integration-run-request/v1",
        "sdi.mobility-requirements-specification/v1",
        "sdi.target-execution-profile/v1",
    ]
    assert all(item["byte_size"] > 0 for item in identified["inputs"])
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in identified["inputs"]
    )


def test_rejects_duplicate_yaml_keys_before_assigning_identity(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    duplicate = RUN_REQUEST.replace(
        "scenario_id: S-04", "scenario_id: S-04\nscenario_id: S-05"
    )
    commit_sha = _replace_and_commit(repository, "runs/request.yaml", duplicate)

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "duplicate")


def test_rejects_unknown_contract_keys(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    unknown_key = RUN_REQUEST.replace(
        "testcase_id: TC-03", "testcase_id: TC-03\nexecution_id: caller-owned"
    )
    commit_sha = _replace_and_commit(repository, "runs/request.yaml", unknown_key)

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "extra inputs are not permitted")


def test_rejects_malformed_requirement_relationships(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    malformed = REQUIREMENTS_SPECIFICATION.replace(
        "      - complete-delivery", "      - missing-need"
    )
    commit_sha = _replace_and_commit(
        repository, "requirements/delivery.yaml", malformed
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "unknown traces_to")


def test_rejects_duplicate_requirement_relationships(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    duplicate_relationship = REQUIREMENTS_SPECIFICATION.replace(
        "      - complete-delivery\n    depends_on:",
        "      - complete-delivery\n      - complete-delivery\n    depends_on:",
    )
    commit_sha = _replace_and_commit(
        repository, "requirements/delivery.yaml", duplicate_relationship
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "traces_to values must be unique")


def test_rejects_value_basis_without_its_required_evidence(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    invalid_basis = REQUIREMENTS_SPECIFICATION.replace(
        "        basis: placeholder\n"
        "        note: Interface-only value; no mobility capability is claimed.",
        "        basis: declared\n"
        "        note: A note cannot replace an accountable source.",
    )
    commit_sha = _replace_and_commit(
        repository, "requirements/delivery.yaml", invalid_basis
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "contract validation failed")


def test_rejects_parent_traversal_in_a_referenced_path(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    traversal = RUN_REQUEST.replace(
        "requirements/delivery.yaml", "../requirements/delivery.yaml"
    )
    commit_sha = _replace_and_commit(repository, "runs/request.yaml", traversal)

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "parent components")


def test_rejects_a_committed_symlink_reference(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    symlink = repository / "profiles/profile-link.yaml"
    symlink.symlink_to("waffle-native-arm64.yaml")
    request = RUN_REQUEST.replace(
        "profiles/waffle-native-arm64.yaml", "profiles/profile-link.yaml"
    )
    (repository / "runs/request.yaml").write_text(request, encoding="utf-8")
    _git(repository, "add", "runs/request.yaml", "profiles/profile-link.yaml")
    _git(repository, "commit", "-m", "Reference a symlink")
    commit_sha = _git(repository, "rev-parse", "HEAD")

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "not a committed regular file")


def test_provenance_uses_immutable_committed_bytes(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, commit_sha = committed_inputs
    (repository / "requirements/delivery.yaml").write_text(
        "dirty worktree content\n", encoding="utf-8"
    )

    first = _identify(repository, commit_sha)
    second = _identify(repository, commit_sha)

    assert first.returncode == second.returncode == 0
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result["execution_id"] != second_result["execution_id"]
    del first_result["execution_id"]
    del second_result["execution_id"]
    assert first_result == second_result
    requirements_provenance = first_result["inputs"][1]
    committed_bytes = REQUIREMENTS_SPECIFICATION.encode()
    assert requirements_provenance["byte_size"] == len(committed_bytes)
    assert (
        requirements_provenance["sha256"] == hashlib.sha256(committed_bytes).hexdigest()
    )


def test_rejects_more_than_one_yaml_document(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    commit_sha = _replace_and_commit(
        repository, "runs/request.yaml", f"{RUN_REQUEST}---\n{{}}\n"
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "exactly one YAML document")


def test_rejects_invalid_utf8(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    commit_sha = _replace_bytes_and_commit(
        repository, "runs/request.yaml", b"schema_version: \xff\n"
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "not valid UTF-8")


def test_rejects_files_larger_than_two_mib(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    oversized = RUN_REQUEST.encode() + b"#" * (2 * 1024 * 1024)
    commit_sha = _replace_bytes_and_commit(repository, "runs/request.yaml", oversized)

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "exceeds 2097152 bytes")


def test_rejects_excessive_yaml_nesting(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    nested = "extra:\n" + "".join(f"{'  ' * depth}nested:\n" for depth in range(1, 34))
    nested += f"{'  ' * 34}value: final\n"
    commit_sha = _replace_and_commit(
        repository, "runs/request.yaml", f"{RUN_REQUEST}{nested}"
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "nesting exceeds")


def test_rejects_unsafe_yaml_tags(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    tagged = RUN_REQUEST.replace("scenario_id: S-04", "scenario_id: !code S-04")
    commit_sha = _replace_and_commit(repository, "runs/request.yaml", tagged)

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "tag is not accepted")


def test_rejects_yaml_anchors_and_aliases(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    anchored = RUN_REQUEST.replace(
        "scenario_id: S-04", "scenario_id: &scenario S-04"
    ).replace("testcase_id: TC-03", "testcase_id: *scenario")
    commit_sha = _replace_and_commit(repository, "runs/request.yaml", anchored)

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "anchors are not accepted")


def test_rejects_non_main_requested_ref(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, commit_sha = committed_inputs
    completed = subprocess.run(
        [
            "sdi-integration",
            "identify-run",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/feature",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            "runs/request.yaml",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    _assert_rejected_without_identity(completed, "refs/heads/main")


def test_rejects_a_commit_that_does_not_match_main(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    _git(repository, "switch", "-c", "unprotected")
    feature_commit = _replace_and_commit(
        repository,
        "profiles/waffle-native-arm64.yaml",
        TARGET_PROFILE.replace("waffle-native-arm64", "unprotected-profile"),
    )

    completed = _identify(repository, feature_commit)

    _assert_rejected_without_identity(completed, "does not match refs/heads/main")


def test_ignores_inherited_git_repository_selectors(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, commit_sha = committed_inputs
    command = [
        "sdi-integration",
        "identify-run",
        "--repository",
        str(repository),
        "--requested-ref",
        "refs/heads/main",
        "--resolved-commit",
        commit_sha,
        "--run-request-path",
        "runs/request.yaml",
    ]

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_COMMON_DIR": str(repository / "not-the-repository")},
    )

    assert completed.returncode == 0, completed.stderr


def test_rejects_non_utf8_origin_without_a_traceback(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, commit_sha = committed_inputs
    subprocess.run(
        [
            b"git",
            b"-C",
            bytes(repository),
            b"config",
            b"remote.origin.url",
            b"https://github.com/example/\xff.git",
        ],
        check=True,
        capture_output=True,
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "origin URL is not valid UTF-8")
    assert "Traceback" not in completed.stderr


def test_accepts_a_declared_zero_gpu_capacity(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    zero_gpu = (
        f"{TARGET_PROFILE}resources:\n"
        "  gpu_count:\n"
        "    value: 0\n"
        "    basis: declared\n"
        "    source: Target operator inventory.\n"
    )
    commit_sha = _replace_and_commit(
        repository, "profiles/waffle-native-arm64.yaml", zero_gpu
    )

    completed = _identify(repository, commit_sha)

    assert completed.returncode == 0, completed.stderr


def test_rejects_explicit_null_for_an_unknown_capacity(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    null_capacity = f"{TARGET_PROFILE}resources:\n  cpu_cores: null\n"
    commit_sha = _replace_and_commit(
        repository, "profiles/waffle-native-arm64.yaml", null_capacity
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "must be omitted when unknown")


def test_extreme_nesting_is_a_controlled_validation_failure(
    committed_inputs: tuple[Path, str],
) -> None:
    repository, _ = committed_inputs
    nested = "extra:\n" + "".join(
        f"{'  ' * depth}nested:\n" for depth in range(1, 1200)
    )
    nested += f"{'  ' * 1200}value: final\n"
    commit_sha = _replace_and_commit(
        repository, "runs/request.yaml", f"{RUN_REQUEST}{nested}"
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "nesting exceeds")
    assert "Traceback" not in completed.stderr


def test_committed_s04_fixtures_preserve_the_accepted_combination_order(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[2]
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

    requirement_path = "requirements/s-04/deliver-book-to-joe.yaml"
    profiles = [
        "waffle-native-arm64",
        "waffle-xycar-amd64",
        "burger-native-arm64",
        "burger-xycar-amd64",
        "waffle-jetson-arm64",
        "burger-jetson-arm64",
    ]
    fixture_paths = [
        f"runs/s-04/s-04-tc-03-c-{number:02}-fixture.yaml" for number in range(1, 7)
    ]
    source_paths = [
        requirement_path,
        *(f"profiles/s-04/{profile}.yaml" for profile in profiles),
        *fixture_paths,
    ]
    for relative_path in source_paths:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source_root / relative_path).read_bytes())

    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add S-04 Fixture inputs")
    commit_sha = _git(repository, "rev-parse", "HEAD")

    for number, (request_path, profile_id) in enumerate(
        zip(fixture_paths, profiles, strict=True), start=1
    ):
        completed = subprocess.run(
            [
                "sdi-integration",
                "identify-run",
                "--repository",
                str(repository),
                "--requested-ref",
                "refs/heads/main",
                "--resolved-commit",
                commit_sha,
                "--run-request-path",
                request_path,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        identified = json.loads(completed.stdout)
        assert identified["combination_id"] == f"C-{number:02}"
        assert identified["profile_id"] == profile_id

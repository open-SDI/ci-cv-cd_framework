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
requirements_specification: requirements/delivery.md
combination:
  combination_id: C-01
  target_profile: profiles/waffle-native-arm64.yaml
"""

REQUIREMENTS_SPECIFICATION = """\
---
schema_version: sdi.mobility-requirements-specification/v1
document_version: 1
requirement_ids:
  - reach-destination
---

# Delivery

## reach-destination

The mobility target shall reach the selected destination.
"""

TARGET_PROFILE = """\
schema_version: sdi.target-execution-profile/v1
profile_id: waffle-native-arm64
provenance:
  basis: placeholder
  note: Test-only profile.
target:
  target_id: turtlebot3-waffle
  kind: sdv
hosts:
  waffle-native:
    role: native
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
        "requirements/delivery.md": REQUIREMENTS_SPECIFICATION,
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


def _run_request_nested_to(depth: int) -> str:
    nested = "extra:\n" + "".join(
        f"{'  ' * level}nested:\n" for level in range(1, depth)
    )
    return f"{RUN_REQUEST}{nested}{'  ' * depth}value: final\n"


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
        "requirements/delivery.md",
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


@pytest.mark.parametrize(
    ("relative_path", "content", "message"),
    [
        pytest.param(
            "runs/request.yaml",
            RUN_REQUEST.replace(
                "scenario_id: S-04", "scenario_id: S-04\nscenario_id: S-05"
            ).encode(),
            "duplicate",
            id="duplicate-key",
        ),
        pytest.param(
            "runs/request.yaml",
            RUN_REQUEST.replace(
                "testcase_id: TC-03", "testcase_id: TC-03\nexecution_id: caller-owned"
            ).encode(),
            "extra inputs are not permitted",
            id="unknown-key",
        ),
        pytest.param(
            "runs/request.yaml",
            RUN_REQUEST.replace(
                "requirements/delivery.md", "../requirements/delivery.md"
            ).encode(),
            "parent components",
            id="parent-traversal",
        ),
        pytest.param(
            "runs/request.yaml",
            f"{RUN_REQUEST}---\n{{}}\n".encode(),
            "exactly one YAML document",
            id="several-documents",
        ),
        pytest.param(
            "runs/request.yaml",
            b"schema_version: \xff\n",
            "not valid UTF-8",
            id="invalid-utf8",
        ),
        pytest.param(
            "runs/request.yaml",
            RUN_REQUEST.encode() + b"#" * (2 * 1024 * 1024),
            "exceeds 2097152 bytes",
            id="larger-than-two-mib",
        ),
        pytest.param(
            "runs/request.yaml",
            RUN_REQUEST.replace(
                "scenario_id: S-04", "scenario_id: !code S-04"
            ).encode(),
            "tag is not accepted",
            id="unsafe-tag",
        ),
        pytest.param(
            "runs/request.yaml",
            RUN_REQUEST.replace("scenario_id: S-04", "scenario_id: &scenario S-04")
            .replace("testcase_id: TC-03", "testcase_id: *scenario")
            .encode(),
            "anchors are not accepted",
            id="anchor-and-alias",
        ),
        pytest.param(
            "profiles/waffle-native-arm64.yaml",
            f"{TARGET_PROFILE}connections:\n  - [waffle-native, orin]\n".encode(),
            "connection must name two distinct profile hosts",
            id="connection-outside-profile-hosts",
        ),
        pytest.param(
            "profiles/waffle-native-arm64.yaml",
            f"{TARGET_PROFILE}orchestrator_provides: null\n".encode(),
            "must be omitted when unknown",
            id="null-orchestrator-inputs",
        ),
    ],
)
def test_rejects_an_invalid_committed_input_before_assigning_identity(
    committed_inputs: tuple[Path, str],
    relative_path: str,
    content: bytes,
    message: str,
) -> None:
    repository, _ = committed_inputs
    commit_sha = _replace_bytes_and_commit(repository, relative_path, content)

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, message)


@pytest.mark.parametrize("depth", [34, 1200], ids=["past-the-limit", "extreme"])
def test_excessive_yaml_nesting_is_a_controlled_validation_failure(
    committed_inputs: tuple[Path, str], depth: int
) -> None:
    repository, _ = committed_inputs
    commit_sha = _replace_and_commit(
        repository, "runs/request.yaml", _run_request_nested_to(depth)
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, "nesting exceeds")
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (
            REQUIREMENTS_SPECIFICATION.removeprefix("---\n"),
            "expected YAML front matter",
        ),
        (
            REQUIREMENTS_SPECIFICATION.replace(
                "  - reach-destination\n",
                "  - reach-destination\n  - reach-destination\n",
            ),
            "requirement_id values must be unique",
        ),
        (
            REQUIREMENTS_SPECIFICATION.replace(
                "  - reach-destination\n", "  - reach-destination\npurpose: x\n"
            ),
            "extra inputs are not permitted",
        ),
    ],
    ids=["no-front-matter", "duplicate-requirement-id", "unknown-field"],
)
def test_rejects_an_invalid_requirements_specification(
    committed_inputs: tuple[Path, str], replacement: str, message: str
) -> None:
    repository, _ = committed_inputs
    commit_sha = _replace_and_commit(
        repository, "requirements/delivery.md", replacement
    )

    completed = _identify(repository, commit_sha)

    _assert_rejected_without_identity(completed, message)


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
    (repository / "requirements/delivery.md").write_text(
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

    requirement_path = "requirements/s-04/deliver-book-to-joe.md"
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

"""Black-box checks for one complete local Pipeline integration run."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import cast

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[2]
S04_COMBINATION_EXPECTATIONS: dict[str, dict[str, str]] = {
    "C-01": {
        "run_request_path": "runs/s-04/s-04-tc-03-c-01-fixture.yaml",
        "profile_id": "waffle-native-arm64",
        "architecture": "arm64",
        "resource_id": "waffle-native-system",
        "location_id": "waffle-native",
        "tier": "mobility",
    },
    "C-02": {
        "run_request_path": "runs/s-04/s-04-tc-03-c-02-fixture.yaml",
        "profile_id": "waffle-xycar-amd64",
        "architecture": "amd64",
        "resource_id": "xycar",
        "location_id": "waffle-xycar",
        "tier": "mobility",
    },
    "C-03": {
        "run_request_path": "runs/s-04/s-04-tc-03-c-03-fixture.yaml",
        "profile_id": "burger-native-arm64",
        "architecture": "arm64",
        "resource_id": "burger-native-system",
        "location_id": "burger-native",
        "tier": "mobility",
    },
    "C-04": {
        "run_request_path": "runs/s-04/s-04-tc-03-c-04-fixture.yaml",
        "profile_id": "burger-xycar-amd64",
        "architecture": "amd64",
        "resource_id": "xycar",
        "location_id": "burger-xycar",
        "tier": "mobility",
    },
    "C-05": {
        "run_request_path": "runs/s-04/s-04-tc-03-c-05-fixture.yaml",
        "profile_id": "waffle-jetson-arm64",
        "architecture": "arm64",
        "resource_id": "jetson-nano",
        "location_id": "waffle-jetson",
        "tier": "edge",
    },
    "C-06": {
        "run_request_path": "runs/s-04/s-04-tc-03-c-06-fixture.yaml",
        "profile_id": "burger-jetson-arm64",
        "architecture": "arm64",
        "resource_id": "jetson-nano",
        "location_id": "burger-jetson",
        "tier": "edge",
    },
}
S04_RUN_REQUEST_PATHS = tuple(
    expected["run_request_path"] for expected in S04_COMBINATION_EXPECTATIONS.values()
)
RUN_REQUEST_PATH = S04_RUN_REQUEST_PATHS[0]
STAGES = ["composition", "image_build", "cv", "cd"]
COMMITTED_FILES = (
    *S04_RUN_REQUEST_PATHS,
    "requirements/s-04/deliver-book-to-joe.yaml",
    "profiles/s-04/waffle-native-arm64.yaml",
    "profiles/s-04/waffle-xycar-amd64.yaml",
    "profiles/s-04/burger-native-arm64.yaml",
    "profiles/s-04/burger-xycar-amd64.yaml",
    "profiles/s-04/waffle-jetson-arm64.yaml",
    "profiles/s-04/burger-jetson-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    "deployment/jenkins/adapters/composition-fixture-v1.yaml",
    "deployment/jenkins/adapters/image-build-fixture-v1.yaml",
    "deployment/jenkins/adapters/cv-fixture-v1.yaml",
    "deployment/jenkins/adapters/cd-fixture-v1.yaml",
    "runs/conformance/composition-negative-domain.yaml",
    "runs/conformance/composition-absent-response.yaml",
    "runs/conformance/composition-handled-failure.yaml",
    "runs/conformance/composition-crash.yaml",
    "runs/conformance/composition-timeout.yaml",
    "runs/conformance/composition-malformed-response.yaml",
    "runs/conformance/composition-missing-output.yaml",
    "runs/conformance/composition-undeclared-output.yaml",
    "runs/conformance/composition-unsafe-output.yaml",
    "runs/conformance/composition-oversize-output.yaml",
    "runs/conformance/composition-response-mismatch.yaml",
    "runs/conformance/composition-schema-mismatch.yaml",
)
VOLATILE_RESULT_FIELDS = {"execution_id", "started_at", "finished_at", "duration_ms"}


def _contract_semantics(value: object) -> object:
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        return {
            key: _contract_semantics(nested)
            for key, nested in mapping.items()
            if key not in VOLATILE_RESULT_FIELDS
        }
    if isinstance(value, list):
        sequence = cast("list[object]", value)
        return [_contract_semantics(nested) for nested in sequence]
    return value


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
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add four-Stage Fixture inputs")
    return repository, _git(repository, "rev-parse", "HEAD")


def _dispatch(
    repository: Path,
    commit_sha: str,
    bundle_root: Path,
    run_request_path: str = RUN_REQUEST_PATH,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "dispatch-local",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            run_request_path,
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _select_adapter(repository: Path, adapter: Path, *, mode: str = "fixture") -> str:
    profile = repository / "integration/stage-profiles/composition-v1.yaml"
    descriptor = repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    descriptor.write_text(
        f"""schema_version: sdi.adapter-descriptor/v1
stage: composition
implementation_mode: {mode}
image: ghcr.io/gurkhaman/sdi-stage-fixture@sha256:{"1" * 64}
entrypoint: {adapter}
process_contract_version: sdi.stage-adapter-process/v1
stage_profile: integration/stage-profiles/composition-v1.yaml
stage_profile_version: sdi.composition-stage-profile/v1
stage_profile_sha256: {hashlib.sha256(profile.read_bytes()).hexdigest()}
agent_label: composition
secret_bindings: []
""",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select conformance adapter")
    return _git(repository, "rev-parse", "HEAD")


def _write_composition_adapter(path: Path, body: str) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest="command", required=True)
run = commands.add_parser("run")
run.add_argument("--request", required=True)
run.add_argument("--input-root", required=True)
run.add_argument("--output-root", required=True)
arguments = parser.parse_args()
request = json.loads(Path(arguments.request).read_text())
output = Path(arguments.output_root)
"""
        + body,
        encoding="utf-8",
    )
    path.chmod(0o755)


def _configure_composition(
    repository: Path, *, mode: str | None = None, work_limit_seconds: int | None = None
) -> str:
    profile = repository / "integration/stage-profiles/composition-v1.yaml"
    descriptor = repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    if work_limit_seconds is not None:
        profile.write_text(
            profile.read_text().replace(
                "work_limit_seconds: 600",
                f"work_limit_seconds: {work_limit_seconds}",
            ),
            encoding="utf-8",
        )
    descriptor_text = descriptor.read_text()
    if mode is not None:
        descriptor_text = descriptor_text.replace(
            "implementation_mode: fixture", f"implementation_mode: {mode}"
        )
    old_digest = next(
        line.split(": ", 1)[1]
        for line in descriptor_text.splitlines()
        if line.startswith("stage_profile_sha256:")
    )
    descriptor.write_text(
        descriptor_text.replace(
            old_digest, hashlib.sha256(profile.read_bytes()).hexdigest()
        ),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Configure composition conformance case")
    return _git(repository, "rev-parse", "HEAD")


@pytest.mark.parametrize("combination_id", S04_COMBINATION_EXPECTATIONS)
def test_dispatches_a_contract_valid_deterministic_four_stage_fixture(
    tmp_path: Path, combination_id: str
) -> None:
    expected = S04_COMBINATION_EXPECTATIONS[combination_id]
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    first_root = tmp_path / "first-bundle"
    second_root = tmp_path / "second-bundle"

    first = _dispatch(repository, commit_sha, first_root, expected["run_request_path"])
    second = _dispatch(
        repository, commit_sha, second_root, expected["run_request_path"]
    )

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result == json.loads(
        (first_root / "pipeline-integration-result.json").read_text()
    )
    assert first_result["execution_id"] != second_result["execution_id"]
    assert _contract_semantics(first_result) == _contract_semantics(second_result)
    assert first_result["kpi_evaluation"] == "not_evaluated"
    assert first_result["combination_id"] == combination_id
    assert first_result["profile_id"] == expected["profile_id"]
    assert "domain_outcome" not in first_result
    assert "overall_verdict" not in first_result
    assert [
        attempt["correlation"]["stage"] for attempt in first_result["attempts"]
    ] == (STAGES)
    assert all(
        attempt["correlation"]["execution_id"] == first_result["execution_id"]
        and attempt["correlation"]["attempt_number"] == 1
        and attempt["lifecycle_state"] == "completed"
        and attempt["execution_conclusion"] == "succeeded"
        and attempt["implementation_mode"] == "fixture"
        and attempt["domain_outcome"] == "not_evaluated"
        for attempt in first_result["attempts"]
    )
    assert [
        [item["slot"] for item in attempt["accepted_inputs"]]
        for attempt in first_result["attempts"]
    ] == [
        ["run_request", "requirements_specification", "target_profile"],
        ["target_profile", "composition_blueprint", "deployment_schema"],
        [
            "requirements_specification",
            "target_profile",
            "composition_blueprint",
            "deployment_schema",
            "image_build_result",
        ],
        [
            "target_profile",
            "composition_blueprint",
            "deployment_schema",
            "image_build_result",
            "validation_evidence",
        ],
    ]

    expected_paths = {
        "pipeline-integration-result.json",
        *(artifact["path"] for artifact in first_result["artifacts"]),
    }
    actual_paths = {
        path.relative_to(first_root).as_posix()
        for path in first_root.rglob("*")
        if path.is_file()
    }
    assert actual_paths == expected_paths
    assert len(first_result["artifacts"]) == 9
    deployment_artifact = next(
        artifact
        for artifact in first_result["artifacts"]
        if artifact["slot"] == "deployment_schema"
    )
    deployment = json.loads(
        (first_root / deployment_artifact["path"]).read_text(encoding="utf-8")
    )
    assert deployment["locations"] == [
        {
            "location_id": expected["location_id"],
            "resource_id": expected["resource_id"],
            "tier": expected["tier"],
        }
    ]
    image_artifact = next(
        artifact
        for artifact in first_result["artifacts"]
        if artifact["slot"] == "image_build_result"
    )
    image_build = json.loads(
        (first_root / image_artifact["path"]).read_text(encoding="utf-8")
    )
    assert {image["architecture"] for image in image_build["images"]} == {
        expected["architecture"]
    }
    for artifact in first_result["artifacts"]:
        first_bytes = (first_root / artifact["path"]).read_bytes()
        second_bytes = (second_root / artifact["path"]).read_bytes()
        assert first_bytes == second_bytes
        assert len(first_bytes) == artifact["byte_size"]
        assert hashlib.sha256(first_bytes).hexdigest() == artifact["sha256"]

    for bundle_root in (first_root, second_root):
        validated = subprocess.run(
            [
                "sdi-integration",
                "validate-bundle",
                "--bundle-root",
                str(bundle_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert validated.returncode == 0, validated.stderr
        assert validated.stdout == ""


def test_handled_execution_failure_publishes_a_complete_result_with_typed_skips(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "handled-failure-adapter"
    _write_composition_adapter(
        adapter,
        """
response = {
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "failed",
    "domain_outcome": "not_evaluated",
    "reason": {
        "category": "fixture",
        "code": "sdi.fixture.handled-failure",
        "summary": "A deterministic handled operational failure.",
    },
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [],
    "diagnostic": {"present": False, "truncated": False},
}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    commit_sha = _select_adapter(repository, adapter)
    bundle_root = tmp_path / "failed-bundle"

    completed = _dispatch(repository, commit_sha, bundle_root)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result == json.loads(
        (bundle_root / "pipeline-integration-result.json").read_text()
    )
    assert [attempt["execution_conclusion"] for attempt in result["attempts"]] == [
        "failed",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert result["attempts"][0]["adapter_response_accepted"] is True
    assert all(
        attempt["reason"]["category"] == "dependency"
        and attempt["accepted_inputs"] == []
        and attempt["accepted_files"] == []
        and "process" not in attempt
        for attempt in result["attempts"][1:]
    )
    assert result["artifacts"] == []


def test_negative_domain_outcome_blocks_dependents_without_machinery_failure(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "negative-domain-adapter"
    blueprint = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-01"
            / "composition-blueprint.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    )
    deployment = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-01"
            / "deployment-schema.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    )
    _write_composition_adapter(
        adapter,
        f"""
(output / "outputs").mkdir()
(output / "outputs/composition-blueprint.json").write_text({blueprint!r})
(output / "outputs/deployment-schema.json").write_text({deployment!r})
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "failed",
    "reason": {{
        "category": "domain",
        "code": "sdi.domain.requirement-unsatisfied",
        "summary": "The evaluated requirement was not satisfied.",
    }},
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": ["composition_blueprint", "deployment_schema"],
    "diagnostic": {{"present": False, "truncated": False}},
}}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    commit_sha = _select_adapter(repository, adapter, mode="implemented")
    bundle_root = tmp_path / "negative-domain-bundle"

    completed = _dispatch(repository, commit_sha, bundle_root)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["execution_conclusion"] == "succeeded"
    assert result["attempts"][0]["domain_outcome"] == "failed"
    assert [attempt["execution_conclusion"] for attempt in result["attempts"][1:]] == [
        "skipped",
        "skipped",
        "skipped",
    ]
    assert {artifact["slot"] for artifact in result["artifacts"]} == {
        "composition_blueprint",
        "deployment_schema",
    }


def test_rejected_candidate_records_runtime_failure_without_salvaging_files(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "undeclared-output-adapter"
    _write_composition_adapter(
        adapter,
        """
(output / "private-endpoint.txt").write_text("https://private.example.test/secret")
(output / "response.json").write_text("not-json")
""",
    )
    commit_sha = _select_adapter(repository, adapter)
    bundle_root = tmp_path / "rejected-candidate-bundle"

    completed = _dispatch(repository, commit_sha, bundle_root)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    first = result["attempts"][0]
    assert first["execution_conclusion"] == "failed"
    assert first["adapter_response_accepted"] is False
    assert first["accepted_files"] == []
    assert result["artifacts"] == []
    assert not any(
        path.read_bytes() == b"https://private.example.test/secret"
        for path in bundle_root.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize(
    ("case_name", "reason_code", "response_status"),
    [
        ("composition-handled-failure", "sdi.fixture.handled-failure", "accepted"),
        ("composition-absent-response", "sdi.adapter.response-absent", "rejected"),
        ("composition-crash", "sdi.adapter.nonzero-exit", "rejected"),
        (
            "composition-malformed-response",
            "sdi.adapter.response-malformed",
            "rejected",
        ),
        ("composition-undeclared-output", "sdi.adapter.undeclared-output", "rejected"),
        ("composition-unsafe-output", "sdi.adapter.unsafe-output", "rejected"),
        ("composition-oversize-output", "sdi.adapter.size-violation", "rejected"),
        ("composition-missing-output", "sdi.adapter.output-missing", "rejected"),
        ("composition-schema-mismatch", "sdi.adapter.schema-mismatch", "rejected"),
        (
            "composition-response-mismatch",
            "sdi.adapter.identity-mismatch",
            "rejected",
        ),
    ],
)
def test_exact_digest_fixture_cases_settle_as_complete_failed_results(
    tmp_path: Path,
    case_name: str,
    reason_code: str,
    response_status: str,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)

    completed = _dispatch(
        repository,
        commit_sha,
        tmp_path / "bundle",
        f"runs/conformance/{case_name}.yaml",
    )

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    attempt = result["attempts"][0]
    assert attempt["reason"]["code"] == reason_code
    assert attempt["adapter_response_accepted"] is (response_status == "accepted")
    assert [item["execution_conclusion"] for item in result["attempts"]] == [
        "failed",
        "skipped",
        "skipped",
        "skipped",
    ]


def test_exact_digest_timeout_case_records_timeout_and_skips(tmp_path: Path) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    commit_sha = _configure_composition(repository, work_limit_seconds=1)

    completed = _dispatch(
        repository,
        commit_sha,
        tmp_path / "bundle",
        "runs/conformance/composition-timeout.yaml",
    )

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    attempt = result["attempts"][0]
    assert attempt["execution_conclusion"] == "timed_out"
    assert attempt["process"]["termination"] == "timed_out"
    assert attempt["reason"]["code"] == "sdi.stage.deadline-exceeded"


def test_stage_timeout_terminates_attached_descendants_and_cleans_work(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "timeout-tree-adapter"
    child_pid_path = tmp_path / "timeout-child.pid"
    _write_composition_adapter(
        adapter,
        f"""
import subprocess
import sys
import time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(child_pid_path)!r}).write_text(str(child.pid))
time.sleep(60)
""",
    )
    _select_adapter(repository, adapter)
    commit_sha = _configure_composition(repository, work_limit_seconds=1)
    bundle_root = tmp_path / "timeout-bundle"

    completed = _dispatch(repository, commit_sha, bundle_root)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["execution_conclusion"] == "timed_out"
    assert not list(tmp_path.glob(".timeout-bundle.*-*"))
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out adapter descendant remained alive")


def test_exact_digest_negative_domain_case_is_not_a_machinery_failure(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)

    completed = _dispatch(
        repository,
        commit_sha,
        tmp_path / "bundle",
        "runs/conformance/composition-negative-domain.yaml",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    attempt = result["attempts"][0]
    assert attempt["execution_conclusion"] == "succeeded"
    assert attempt["domain_outcome"] == "failed"
    assert attempt["reason"]["category"] == "domain"


def test_external_cancellation_terminates_the_process_tree_without_a_result(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "cancellable-adapter"
    child_pid_path = tmp_path / "child.pid"
    ready_path = tmp_path / "ready"
    _write_composition_adapter(
        adapter,
        f"""
import subprocess
import sys
import time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(child_pid_path)!r}).write_text(str(child.pid))
Path({str(ready_path)!r}).write_text("ready")
time.sleep(60)
""",
    )
    commit_sha = _select_adapter(repository, adapter)
    bundle_root = tmp_path / "cancelled-bundle"
    process = subprocess.Popen(
        [
            "sdi-integration",
            "dispatch-local",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            RUN_REQUEST_PATH,
            "--bundle-root",
            str(bundle_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready_path.exists()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 128 + signal.SIGTERM, stderr
    assert stdout == ""
    assert not bundle_root.exists()
    assert not list(tmp_path.glob(".cancelled-bundle.*-*"))
    assert not (tmp_path / ".cancelled-bundle.claim").exists()
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("cancelled adapter descendant remained alive")


def test_not_implemented_stage_and_dependents_are_explicitly_skipped(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    commit_sha = _configure_composition(repository, mode="not_implemented")

    completed = _dispatch(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["implementation_mode"] == "not_implemented"
    assert result["attempts"][0]["reason"]["code"] == "sdi.stage.not-implemented"
    assert all(
        attempt["execution_conclusion"] == "skipped" for attempt in result["attempts"]
    )


def test_implemented_adapter_cannot_consume_unevaluated_fixture_output(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    descriptor = repository / "deployment/jenkins/adapters/image-build-fixture-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            "implementation_mode: fixture", "implementation_mode: implemented"
        ),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select implemented image-build adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")

    completed = _dispatch(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["execution_conclusion"] == "succeeded"
    blocked = result["attempts"][1]
    assert blocked["execution_conclusion"] == "skipped"
    assert blocked["reason"]["code"] == "sdi.dependency.fixture-evidence"
    assert "process" not in blocked


def test_implemented_composition_output_may_feed_a_later_fixture(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "implemented-composition-adapter"
    blueprint = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-01"
            / "composition-blueprint.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    )
    deployment = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-01"
            / "deployment-schema.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    )
    _write_composition_adapter(
        adapter,
        f"""
(output / "outputs").mkdir()
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
    commit_sha = _select_adapter(repository, adapter, mode="implemented")

    completed = _dispatch(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    composition = result["attempts"][0]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["implementation_mode"] == "implemented"
    assert "reason" not in composition
    image_build = result["attempts"][1]
    assert image_build["execution_conclusion"] == "succeeded"
    assert image_build["adapter_response_accepted"] is True
    assert image_build["reason"]["code"] == (
        "sdi.fixture.image-build-after-implemented-composition"
    )
    assert result["attempts"][2]["reason"]["code"] == "sdi.fixture.input-mismatch"


def test_process_loss_records_runtime_failure_and_skips(tmp_path: Path) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    descriptor = repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            "entrypoint: sdi-fixture-adapter",
            "entrypoint: /missing/sdi-fixture-adapter",
        ),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select missing adapter process")
    commit_sha = _git(repository, "rev-parse", "HEAD")

    completed = _dispatch(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    attempt = json.loads(completed.stdout)["attempts"][0]
    assert attempt["reason"]["code"] == "sdi.adapter.process-lost"
    assert attempt["process"]["termination"] == "lost"
    assert attempt["adapter_response_accepted"] is False


def test_process_signal_records_runtime_failure_and_skips(tmp_path: Path) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "signaled-adapter"
    _write_composition_adapter(
        adapter,
        """
import os
import signal
os.kill(os.getpid(), signal.SIGKILL)
""",
    )
    commit_sha = _select_adapter(repository, adapter)

    completed = _dispatch(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    attempt = json.loads(completed.stdout)["attempts"][0]
    assert attempt["reason"]["code"] == "sdi.adapter.process-signaled"
    assert attempt["process"]["termination"] == "signaled"
    assert attempt["process"]["signal"] == signal.SIGKILL
    assert attempt["adapter_response_accepted"] is False


@pytest.mark.parametrize(
    "sentinel",
    [
        "https://10.0.0.8/private?token=credential",
        "/home/acceptance-user/private/controller.log",
        "/var/lib/jenkins/secrets/controller.log",
        r"C:\Users\acceptance-user\private\controller.log",
        "jenkins.internal",
    ],
)
def test_sensitive_diagnostic_is_not_accepted_or_archived(
    tmp_path: Path, sentinel: str
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "sensitive-diagnostic-adapter"
    _write_composition_adapter(
        adapter,
        f"""
(output / "diagnostic.txt").write_text({sentinel!r})
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "failed",
    "domain_outcome": "not_evaluated",
    "reason": {{
        "category": "fixture",
        "code": "sdi.fixture.handled-failure",
        "summary": "A handled conformance failure.",
    }},
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [],
    "diagnostic": {{"present": True, "truncated": False}},
}}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    commit_sha = _select_adapter(repository, adapter)
    bundle_root = tmp_path / "bundle"

    completed = _dispatch(repository, commit_sha, bundle_root)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["adapter_response_accepted"] is False
    assert result["artifacts"] == []
    assert (
        sentinel not in (bundle_root / "pipeline-integration-result.json").read_text()
    )


@pytest.mark.parametrize(
    "sentinel",
    ["password=acceptance-sentinel", "/var/lib/jenkins/config.xml", "jenkins.internal"],
)
def test_sensitive_domain_output_is_not_accepted_or_archived(
    tmp_path: Path, sentinel: str
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    adapter = tmp_path / "sensitive-domain-output-adapter"
    blueprint = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-01"
            / "composition-blueprint.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
        .replace('"version": "1"', f'"version": "{sentinel}"', 1)
    )
    deployment = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-01"
            / "deployment-schema.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    )
    _write_composition_adapter(
        adapter,
        f"""
(output / "outputs").mkdir()
(output / "outputs/composition-blueprint.json").write_text({blueprint!r})
(output / "outputs/deployment-schema.json").write_text({deployment!r})
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "failed",
    "reason": {{
        "category": "domain",
        "code": "sdi.domain.requirement-unsatisfied",
        "summary": "The evaluated requirement was not satisfied.",
    }},
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": ["composition_blueprint", "deployment_schema"],
    "diagnostic": {{"present": False, "truncated": False}},
}}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    commit_sha = _select_adapter(repository, adapter, mode="implemented")
    bundle_root = tmp_path / "bundle"

    completed = _dispatch(repository, commit_sha, bundle_root)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["adapter_response_accepted"] is False
    assert result["artifacts"] == []
    assert (
        sentinel not in (bundle_root / "pipeline-integration-result.json").read_text()
    )


def test_bundle_validation_rejects_tampered_artifact_bytes(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result = json.loads(dispatched.stdout)
    artifact_path = bundle_root / result["artifacts"][0]["path"]
    artifact_path.write_bytes(b"tampered\n")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert validated.stdout == ""
    assert "artifact" in validated.stderr.lower()


@pytest.mark.parametrize("mutation", ["missing", "extra", "unsafe"])
def test_bundle_validation_rejects_invalid_archive_inventory(
    tmp_path: Path, mutation: str
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result = json.loads(dispatched.stdout)
    artifact_path = bundle_root / result["artifacts"][0]["path"]
    if mutation == "missing":
        artifact_path.unlink()
    elif mutation == "extra":
        (bundle_root / "extra.txt").write_text("undeclared archive file")
    else:
        artifact_path.unlink()
        artifact_path.symlink_to("/dev/null")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert validated.stdout == ""


def test_bundle_validation_rejects_unbounded_artifact_grants(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    result["attempts"][0]["accepted_files"][0]["byte_size"] = 32769
    result["artifacts"][0]["byte_size"] = 32769
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "less than or equal to 32768" in validated.stderr


def test_bundle_validation_rejects_machine_specific_provenance_paths(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    original = result["input_provenance"][0]["path"]
    machine_path = "/home/acceptance-user/request.yaml"
    result["input_provenance"][0]["path"] = machine_path
    for attempt in result["attempts"]:
        for accepted_input in attempt["accepted_inputs"]:
            if accepted_input["source_path"] == original:
                accepted_input["source_path"] = machine_path
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert validated.stdout == ""
    assert "repository-relative" in validated.stderr


@pytest.mark.parametrize(
    "repository_identity",
    ["jenkins.internal/repository", "buildhost/repository"],
)
def test_bundle_validation_rejects_a_private_repository_identity(
    tmp_path: Path, repository_identity: str
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["repository"] = repository_identity
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "repository" in validated.stderr


def test_bundle_validation_rejects_sensitive_domain_content(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    artifact_path = bundle_root / "stages/composition/composition-blueprint.json"
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    document["services"][0]["version"] = "jenkins.internal"
    content = (
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    artifact_path.write_bytes(content)
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    for artifact in result["artifacts"]:
        if artifact["path"] == "stages/composition/composition-blueprint.json":
            artifact["byte_size"] = len(content)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
    for attempt in result["attempts"]:
        for accepted in attempt["accepted_files"]:
            if accepted.get("slot") == "composition_blueprint":
                accepted["byte_size"] = len(content)
                accepted["sha256"] = hashlib.sha256(content).hexdigest()
        for accepted in attempt["accepted_inputs"]:
            if accepted["slot"] == "composition_blueprint":
                accepted["byte_size"] = len(content)
                accepted["sha256"] = hashlib.sha256(content).hexdigest()
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "sensitive material" in validated.stderr


def test_bundle_validation_rejects_a_stage_output_under_the_wrong_schema(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    result["attempts"][1]["accepted_files"][0]["schema_version"] = (
        "sdi.deployment-result/v1"
    )
    result["artifacts"][3]["schema_version"] = "sdi.deployment-result/v1"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "reviewed profile" in validated.stderr


def test_bundle_validation_rechecks_cross_stage_domain_correlation(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    artifact = next(
        item for item in result["artifacts"] if item["slot"] == "deployment_result"
    )
    domain_path = bundle_root / artifact["path"]
    domain_output = json.loads(domain_path.read_text())
    domain_output["profile_id"] = "another-profile"
    changed_bytes = json.dumps(domain_output).encode()
    domain_path.write_bytes(changed_bytes)
    changed_sha256 = hashlib.sha256(changed_bytes).hexdigest()
    artifact["byte_size"] = len(changed_bytes)
    artifact["sha256"] = changed_sha256
    accepted = result["attempts"][3]["accepted_files"][0]
    accepted["byte_size"] = len(changed_bytes)
    accepted["sha256"] = changed_sha256
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "correlation" in validated.stderr.lower()


def test_bundle_validation_rejects_work_after_a_negative_domain_outcome(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    first = result["attempts"][0]
    first["domain_outcome"] = "failed"
    first["reason"] = {
        "category": "domain",
        "code": "sdi.domain.requirement-unsatisfied",
        "summary": "The represented Domain requirement was not satisfied.",
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "must be skipped" in validated.stderr


def test_bundle_validation_rejects_ascii_delete_in_diagnostics(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    artifact = next(
        item
        for item in result["artifacts"]
        if item["stage"] == "composition" and item["role"] == "diagnostic"
    )
    diagnostic_path = bundle_root / artifact["path"]
    changed_bytes = diagnostic_path.read_bytes() + b"\x7f"
    diagnostic_path.write_bytes(changed_bytes)
    changed_sha256 = hashlib.sha256(changed_bytes).hexdigest()
    artifact["byte_size"] = len(changed_bytes)
    artifact["sha256"] = changed_sha256
    accepted = result["attempts"][0]["accepted_files"][2]
    accepted["byte_size"] = len(changed_bytes)
    accepted["sha256"] = changed_sha256
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "sanitized" in validated.stderr.lower()

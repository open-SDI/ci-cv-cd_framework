"""Black-box checks for one complete local Pipeline integration run."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest

from sdi_pipeline_integration._local_dispatch import dispatch_local
from sdi_pipeline_integration._stage_profiles import stage_profile_bytes
from sdi_pipeline_integration._stage_registry import (
    EXPECTED_STAGE_INPUT_SLOTS,
    STAGE_ORDER,
    STAGES,
)
from sdi_pipeline_integration._stage_runtime import (
    ADAPTER_SHUTDOWN_GRACE_SECONDS,
    SuppliedInputs,
)
from tests._fixture_inputs import (
    FIXTURE_COMPOSITION_CONFIG,
    FIXTURE_SERVICE_REPOSITORY,
    RED_RUN_EXIT,
    RUN_REQUEST_PATH,
    S04_COMBINATION_EXPECTATIONS,
    SUPPLIED_INPUT_ARGUMENTS,
    commit_fixture_repository,
    dispatch_local_cli,
    run_git,
    write_fixture_composition_descriptor,
    write_scripted_adapter,
)

SOURCE_ROOT = Path(__file__).resolve().parents[2]
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


def _select_adapter(repository: Path, adapter: Path, *, mode: str = "fixture") -> str:
    write_fixture_composition_descriptor(repository, entrypoint=adapter, mode=mode)
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Select conformance adapter")
    return run_git(repository, "rev-parse", "HEAD")


def _configure_composition(repository: Path, *, mode: str) -> str:
    descriptor = repository / "deployment/jenkins/adapters/composition-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            "implementation_mode: fixture", f"implementation_mode: {mode}"
        ),
        encoding="utf-8",
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Configure composition conformance case")
    return run_git(repository, "rev-parse", "HEAD")


def _dispatch_with_composition_limit(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    bundle_root: Path,
    run_request_path: str = RUN_REQUEST_PATH,
) -> dict[str, Any]:
    """Run in process so the registry's one-second limit reaches the run."""
    composition = STAGES["composition"]
    monkeypatch.setitem(
        STAGES,
        "composition",
        dataclasses.replace(
            composition,
            grant=dataclasses.replace(composition.grant, work_limit_seconds=1),
        ),
    )
    (repository / "integration/stage-profiles/composition-v1.yaml").write_bytes(
        stage_profile_bytes("composition")
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Shorten the composition work limit")
    return dispatch_local(
        repository_path=repository,
        requested_ref="refs/heads/main",
        resolved_commit=run_git(repository, "rev-parse", "HEAD"),
        run_request_path=run_request_path,
        bundle_root=bundle_root,
        supplied=SuppliedInputs(
            service_repository=FIXTURE_SERVICE_REPOSITORY,
            composition_config=FIXTURE_COMPOSITION_CONFIG,
        ),
    )


@pytest.mark.parametrize("combination_id", S04_COMBINATION_EXPECTATIONS)
def test_dispatches_a_contract_valid_deterministic_four_stage_fixture(
    tmp_path: Path, combination_id: str
) -> None:
    expected = S04_COMBINATION_EXPECTATIONS[combination_id]
    repository, commit_sha = commit_fixture_repository(tmp_path)
    first_root = tmp_path / "first-bundle"
    second_root = tmp_path / "second-bundle"

    first = dispatch_local_cli(
        repository, commit_sha, first_root, expected["run_request_path"]
    )
    second = dispatch_local_cli(
        repository, commit_sha, second_root, expected["run_request_path"]
    )

    assert first.returncode == second.returncode == RED_RUN_EXIT, (
        first.stderr + second.stderr
    )
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
    ] == list(STAGE_ORDER)
    assert all(
        attempt["correlation"]["execution_id"] == first_result["execution_id"]
        and attempt["correlation"]["attempt_number"] == 1
        and attempt["domain_outcome"] == "not_evaluated"
        for attempt in first_result["attempts"]
    )
    assert [
        (
            attempt["lifecycle_state"],
            attempt["execution_conclusion"],
            attempt["implementation_mode"],
            attempt["reason"]["code"],
        )
        for attempt in first_result["attempts"]
    ] == [
        ("completed", "succeeded", "fixture", "sdi.fixture.composition-executed"),
        ("completed", "succeeded", "fixture", "sdi.fixture.image-build-executed"),
        ("skipped", "skipped", "implemented", "sdi.dependency.fixture-evidence"),
        ("skipped", "skipped", "fixture", "sdi.dependency.prerequisite-blocked"),
    ]
    assert [
        [item["slot"] for item in attempt["accepted_inputs"]]
        for attempt in first_result["attempts"]
    ] == [
        list(EXPECTED_STAGE_INPUT_SLOTS["composition"]),
        list(EXPECTED_STAGE_INPUT_SLOTS["image_build"]),
        [],
        [],
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
    assert [
        (artifact["stage"], artifact["slot"]) for artifact in first_result["artifacts"]
    ] == [
        ("composition", "composition_blueprint"),
        ("composition", "deployment_schema"),
        ("composition", "diagnostic"),
        ("image_build", "image_build_result"),
        ("image_build", "diagnostic"),
    ]
    deployment_artifact = next(
        artifact
        for artifact in first_result["artifacts"]
        if artifact["slot"] == "deployment_schema"
    )
    deployment = json.loads(
        (first_root / deployment_artifact["path"]).read_text(encoding="utf-8")
    )
    assert {item["host"] for item in deployment["placements"]} == {expected["host"]}
    image_artifact = next(
        artifact
        for artifact in first_result["artifacts"]
        if artifact["slot"] == "image_build_result"
    )
    image_build = json.loads(
        (first_root / image_artifact["path"]).read_text(encoding="utf-8")
    )
    assert {image["status"] for image in image_build["images"]} == {"not_evaluated"}
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
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "handled-failure-adapter"
    write_scripted_adapter(
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

    completed = dispatch_local_cli(repository, commit_sha, bundle_root)

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
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "negative-domain-adapter"
    write_scripted_adapter(
        adapter,
        """
response = {
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "failed",
    "reason": {
        "category": "domain",
        "code": "sdi.domain.requirement-unsatisfied",
        "summary": "The evaluated requirement was not satisfied.",
    },
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [],
    "diagnostic": {"present": False, "truncated": False},
}
(output / "response.json").write_text(json.dumps(response))
""",
    )
    commit_sha = _select_adapter(repository, adapter, mode="implemented")
    bundle_root = tmp_path / "negative-domain-bundle"

    completed = dispatch_local_cli(repository, commit_sha, bundle_root)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["execution_conclusion"] == "succeeded"
    assert result["attempts"][0]["domain_outcome"] == "failed"
    assert result["attempts"][0]["reason"]["category"] == "domain"
    assert [attempt["execution_conclusion"] for attempt in result["attempts"][1:]] == [
        "skipped",
        "skipped",
        "skipped",
    ]
    assert result["artifacts"] == []


def test_negative_domain_outcome_cannot_publish_domain_output(
    tmp_path: Path,
) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "negative-domain-output-adapter"
    case = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
    implemented = ('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    blueprint = (case / "composition-blueprint.json").read_text().replace(*implemented)
    deployment = (case / "deployment-schema.json").read_text().replace(*implemented)
    write_scripted_adapter(
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

    completed = dispatch_local_cli(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["adapter_response_accepted"] is False
    assert result["artifacts"] == []


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
    repository, commit_sha = commit_fixture_repository(tmp_path)

    completed = dispatch_local_cli(
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
    if response_status == "rejected":
        # A rejected response accepts none of the files the adapter left.
        assert attempt["accepted_files"] == []
        assert result["artifacts"] == []
    assert [item["execution_conclusion"] for item in result["attempts"]] == [
        "failed",
        "skipped",
        "skipped",
        "skipped",
    ]


@pytest.mark.slow
def test_stage_timeout_terminates_attached_descendants_and_cleans_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "timeout-tree-adapter"
    child_pid_path = tmp_path / "timeout-child.pid"
    write_scripted_adapter(
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
    bundle_root = tmp_path / "timeout-bundle"

    started = time.monotonic()
    result = _dispatch_with_composition_limit(monkeypatch, repository, bundle_root)

    # The terminated adapter is reaped, so its exit does not wait out the grace.
    assert time.monotonic() - started < ADAPTER_SHUTDOWN_GRACE_SECONDS
    attempt = result["attempts"][0]
    assert attempt["execution_conclusion"] == "timed_out"
    assert attempt["process"]["termination"] == "timed_out"
    assert attempt["reason"]["code"] == "sdi.stage.deadline-exceeded"
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


def test_external_cancellation_terminates_the_process_tree_without_a_result(
    tmp_path: Path,
) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "cancellable-adapter"
    child_pid_path = tmp_path / "child.pid"
    ready_path = tmp_path / "ready"
    write_scripted_adapter(
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
            *SUPPLIED_INPUT_ARGUMENTS,
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
    repository, _ = commit_fixture_repository(tmp_path)
    commit_sha = _configure_composition(repository, mode="not_implemented")

    completed = dispatch_local_cli(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["implementation_mode"] == "not_implemented"
    assert result["attempts"][0]["reason"]["code"] == "sdi.stage.not-implemented"
    assert all(
        attempt["execution_conclusion"] == "skipped" for attempt in result["attempts"]
    )


def test_rejects_a_key_the_composition_descriptor_does_not_bind(
    tmp_path: Path,
) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    write_fixture_composition_descriptor(repository, mode="implemented")
    descriptor = repository / "deployment/jenkins/adapters/composition-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            "secret_bindings: []", "secret_bindings: [OTHER_KEY]"
        )
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Bind a key the config does not name")

    completed = dispatch_local_cli(
        repository, run_git(repository, "rev-parse", "HEAD"), tmp_path / "bundle"
    )

    assert completed.returncode == 2
    assert "does not bind" in completed.stderr
    assert not (tmp_path / "bundle").exists()


def test_implemented_adapter_cannot_consume_unevaluated_fixture_output(
    tmp_path: Path,
) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    descriptor = repository / "deployment/jenkins/adapters/image-build-fixture-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            "implementation_mode: fixture", "implementation_mode: implemented"
        ),
        encoding="utf-8",
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Select implemented image-build adapter")
    commit_sha = run_git(repository, "rev-parse", "HEAD")

    completed = dispatch_local_cli(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["execution_conclusion"] == "succeeded"
    blocked = result["attempts"][1]
    assert blocked["execution_conclusion"] == "skipped"
    assert blocked["reason"]["code"] == "sdi.dependency.fixture-evidence"
    assert "process" not in blocked


def test_fixture_stage_skips_implemented_composition_output(
    tmp_path: Path,
) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "implemented-composition-adapter"
    case = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
    implemented = ('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    blueprint = (case / "composition-blueprint.json").read_text().replace(*implemented)
    deployment = (case / "deployment-schema.json").read_text().replace(*implemented)
    write_scripted_adapter(
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

    completed = dispatch_local_cli(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, cv, cd = result["attempts"]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["implementation_mode"] == "implemented"
    assert "reason" not in composition
    assert image_build["lifecycle_state"] == "skipped"
    assert image_build["reason"]["code"] == "sdi.dependency.implemented-evidence"
    assert "process" not in image_build
    assert [cv["reason"]["code"], cd["reason"]["code"]] == [
        "sdi.dependency.prerequisite-blocked",
        "sdi.dependency.prerequisite-blocked",
    ]


def test_process_loss_records_runtime_failure_and_skips(tmp_path: Path) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    descriptor = repository / "deployment/jenkins/adapters/composition-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            "entrypoint: sdi-fixture-adapter",
            "entrypoint: /missing/sdi-fixture-adapter",
        ),
        encoding="utf-8",
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Select missing adapter process")
    commit_sha = run_git(repository, "rev-parse", "HEAD")

    completed = dispatch_local_cli(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    attempt = json.loads(completed.stdout)["attempts"][0]
    assert attempt["reason"]["code"] == "sdi.adapter.process-lost"
    assert attempt["process"]["termination"] == "lost"
    assert attempt["adapter_response_accepted"] is False


def test_process_signal_records_runtime_failure_and_skips(tmp_path: Path) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "signaled-adapter"
    write_scripted_adapter(
        adapter,
        """
import os
import signal
os.kill(os.getpid(), signal.SIGKILL)
""",
    )
    commit_sha = _select_adapter(repository, adapter)

    completed = dispatch_local_cli(repository, commit_sha, tmp_path / "bundle")

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
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "sensitive-diagnostic-adapter"
    write_scripted_adapter(
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

    completed = dispatch_local_cli(repository, commit_sha, bundle_root)

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
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "sensitive-domain-output-adapter"
    blueprint = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
            / "composition-blueprint.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
        .replace('"capability": "localization"', f'"capability": "{sentinel}"', 1)
    )
    deployment = (
        (
            SOURCE_ROOT
            / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
            / "deployment-schema.json"
        )
        .read_text()
        .replace('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    )
    write_scripted_adapter(
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
    bundle_root = tmp_path / "bundle"

    completed = dispatch_local_cli(repository, commit_sha, bundle_root)

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["adapter_response_accepted"] is False
    assert result["artifacts"] == []
    assert (
        sentinel not in (bundle_root / "pipeline-integration-result.json").read_text()
    )


def test_composition_placing_a_service_outside_the_profile_hosts_is_not_accepted(
    tmp_path: Path,
) -> None:
    repository, _ = commit_fixture_repository(tmp_path)
    adapter = tmp_path / "unknown-host-adapter"
    case = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
    implemented = ('"evidence_basis": "fixture"', '"evidence_basis": "implemented"')
    blueprint = (case / "composition-blueprint.json").read_text().replace(*implemented)
    deployment = (
        (case / "deployment-schema.json")
        .read_text()
        .replace(*implemented)
        .replace('"host": "orin"', '"host": "xycar"', 1)
    )
    write_scripted_adapter(
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

    completed = dispatch_local_cli(repository, commit_sha, tmp_path / "bundle")

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["attempts"][0]["adapter_response_accepted"] is False
    assert result["artifacts"] == []

"""Black-box checks for the public CLI seams used by Jenkins."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_ID = "12345678-1234-4234-9234-123456789abc"
SUCCESS_REQUEST = "runs/s-04/s-04-tc-03-c-01-fixture.yaml"
DESCRIPTORS = (
    ("composition", "deployment/jenkins/adapters/composition-fixture-v1.yaml"),
    ("image_build", "deployment/jenkins/adapters/image-build-fixture-v1.yaml"),
    ("cv", "deployment/jenkins/adapters/cv-fixture-v1.yaml"),
    ("cd", "deployment/jenkins/adapters/cd-fixture-v1.yaml"),
)
WORK_LIMITS = {
    "composition": "600",
    "image_build": "1800",
    "cv": "2700",
    "cd": "900",
}
COMMITTED_FILES = (
    SUCCESS_REQUEST,
    "requirements/s-04/deliver-book-to-joe.yaml",
    "profiles/s-04/waffle-native-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    "deployment/jenkins/adapters/composition-fixture-v1.yaml",
    "deployment/jenkins/adapters/image-build-fixture-v1.yaml",
    "deployment/jenkins/adapters/cv-fixture-v1.yaml",
    "deployment/jenkins/adapters/cd-fixture-v1.yaml",
    "runs/conformance/composition-negative-domain.yaml",
    "runs/conformance/composition-handled-failure.yaml",
    "runs/conformance/composition-malformed-response.yaml",
    "runs/conformance/composition-timeout.yaml",
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
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


def _preflight(
    repository: Path, commit_sha: str, request_path: str, started_millis: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "preflight-jenkins-run",
            *_common(repository, commit_sha, request_path),
            "--handoff-contract-version",
            "sdi.github-jenkins-handoff/v1",
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--build-started-at-millis",
            started_millis,
            "--integration-node",
            "integration",
            "--composition-node",
            "ci",
            "--image-build-node",
            "image-build",
            "--cv-node",
            "cv",
            "--cd-node",
            "cd",
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
    run_deadline_millis: int | None = None,
    work_limits: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    preflight = _preflight(repository, commit_sha, request_path, started_millis)
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout)["execution_id"] == EXECUTION_ID

    attempts: list[Path] = []
    deadline = str(run_deadline_millis or int(time.time() * 1000) + 60_000)
    configured_work_limits = WORK_LIMITS | (work_limits or {})
    for stage, descriptor in DESCRIPTORS:
        attempt_root = root / "transfers" / stage
        command = [
            "sdi-integration",
            "execute-jenkins-stage",
            *_common(repository, commit_sha, request_path),
            "--descriptor-path",
            descriptor,
            "--attempt-root",
            str(attempt_root),
            "--work-limit-seconds",
            configured_work_limits[stage],
            "--run-deadline-epoch-millis",
            deadline,
        ]
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
    repository, commit_sha = _repository(tmp_path)
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
        stage for stage, _descriptor in DESCRIPTORS
    ]
    assert len(result["artifacts"]) == 9
    assert all(
        attempt["execution_conclusion"] == "succeeded" for attempt in result["attempts"]
    )


def test_malformed_final_bundle_is_rejected_by_public_cli(tmp_path: Path) -> None:
    repository, commit_sha = _repository(tmp_path)
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


def test_distributed_timeout_records_one_attempt_and_skips(tmp_path: Path) -> None:
    repository, commit_sha = _repository(tmp_path)

    conclusion, result = _execute_chain(
        repository,
        commit_sha,
        "runs/conformance/composition-timeout.yaml",
        tmp_path / "run",
        str(int((time.time() - 1) * 1000)),
        int(time.time() * 1000) + 2_000,
    )

    assert conclusion.returncode == 1, conclusion.stderr
    assert result["attempts"][0]["execution_conclusion"] == "timed_out"
    assert result["attempts"][0]["reason"]["code"] == "sdi.run.deadline-exceeded"
    assert all(
        attempt["execution_conclusion"] == "skipped"
        for attempt in result["attempts"][1:]
    )


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
            "--descriptor-path",
            DESCRIPTORS[0][1],
            "--attempt-root",
            str(attempt),
            "--work-limit-seconds",
            WORK_LIMITS["composition"],
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
            "--descriptor-path",
            DESCRIPTORS[0][1],
            "--attempt-root",
            str(attempt),
            "--work-limit-seconds",
            WORK_LIMITS["composition"],
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
    descriptor = repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
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
            "--descriptor-path",
            DESCRIPTORS[0][1],
            "--attempt-root",
            str(attempt),
            "--work-limit-seconds",
            WORK_LIMITS["composition"],
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

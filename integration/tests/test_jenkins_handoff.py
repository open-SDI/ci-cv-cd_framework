"""Black-box checks for the GitHub-to-Jenkins handoff CLI seam."""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, cast, final, override
from urllib.parse import parse_qs, urlsplit

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

SOURCE_ROOT = Path(__file__).resolve().parents[2]
RUN_REQUEST_PATH = "runs/s-04/s-04-tc-03-c-01-fixture.yaml"
COMMITTED_FILES = (
    RUN_REQUEST_PATH,
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
    _ = _git(repository, "init", "--initial-branch=main")
    _ = _git(repository, "config", "user.name", "Test Operator")
    _ = _git(repository, "config", "user.email", "operator@example.test")
    _ = _git(
        repository,
        "remote",
        "add",
        "origin",
        "https://github.com/example/sdi-fixture.git",
    )
    for relative_path in COMMITTED_FILES:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_bytes((SOURCE_ROOT / relative_path).read_bytes())
    _ = _git(repository, "add", ".")
    _ = _git(repository, "commit", "-m", "Add handoff Fixture inputs")
    return repository, _git(repository, "rev-parse", "HEAD")


def _valid_bundle(
    repository: Path, commit_sha: str, bundle_root: Path
) -> tuple[str, dict[str, bytes]]:
    completed = subprocess.run(
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
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    result = cast("dict[str, object]", json.loads(completed.stdout))
    files = {
        path.relative_to(bundle_root).as_posix(): path.read_bytes()
        for path in bundle_root.rglob("*")
        if path.is_file()
    }
    return cast("str", result["execution_id"]), files


@final
class FakeJenkins:
    """A controlled Jenkins core REST surface for handoff tests."""

    def __init__(
        self,
        artifacts: dict[str, bytes],
        *,
        mode: str = "success",
        jenkins_result: str = "SUCCESS",
    ) -> None:
        """Initialize exact artifacts and expected run identity."""
        self.artifacts: dict[str, bytes] = artifacts
        self.mode: str = mode
        self.jenkins_result: str = jenkins_result
        self.base_url: str = ""
        self.requests: list[tuple[str, str, str | None]] = []
        self.submitted_parameters: dict[str, list[str]] = {}
        self.queue_polls: int = 0
        self.build_polls: int = 0
        self.cancel_requests: list[str] = []
        self.cancelled: bool = False

    def handler(self) -> type[BaseHTTPRequestHandler]:  # noqa: C901
        """Create a request handler bound to this fake's state."""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            @override
            def log_message(self, format: str, *arguments: object) -> None:
                _ = format, arguments

            def _record(self) -> None:
                fake.requests.append(
                    (self.command, self.path, self.headers.get("Authorization"))
                )

            def _json(self, value: object, status: int = 200) -> None:
                body = json.dumps(value, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                _ = self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: C901, PLR0911, PLR0912, PLR0915
                self._record()
                split = urlsplit(self.path)
                if split.path == "/api/json":
                    if fake.mode == "reachability":
                        cast("socket.socket", self.connection).shutdown(
                            socket.SHUT_RDWR
                        )
                        return
                    if fake.mode == "authentication":
                        self.send_error(401)
                        return
                    self._json({"mode": "NORMAL"})
                    return
                if split.path == "/whoAmI/api/json":
                    if fake.mode == "identity_mismatch":
                        self._json({"name": "anonymous", "authenticated": False})
                        return
                    self._json({"name": "github-handoff", "authenticated": True})
                    return
                if split.path == "/job/pipeline-integration/api/json":
                    if fake.mode == "authorization":
                        self.send_error(403)
                        return
                    self._json(
                        {
                            "name": "pipeline-integration",
                            "url": f"{fake.base_url}/job/pipeline-integration/",
                        }
                    )
                    return
                if split.path == "/queue/item/42/api/json":
                    fake.queue_polls += 1
                    if fake.mode == "queue_missing":
                        self.send_error(404)
                        return
                    if fake.mode == "http_header_deadline" and not fake.cancelled:
                        try:
                            _ = self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                            self.wfile.flush()
                            for _index in range(20):
                                _ = self.wfile.write(b"X-Drip: value\r\n")
                                self.wfile.flush()
                                time.sleep(0.1)
                        except BrokenPipeError:
                            pass
                        return
                    if fake.mode == "http_deadline":
                        time.sleep(1.2)
                    if fake.cancelled or fake.mode == "queue_cancelled":
                        self._json({"id": 42, "cancelled": True})
                    elif (
                        fake.mode in {"queue_deadline", "http_deadline"}
                        or fake.queue_polls == 1
                    ):
                        self._json({"id": 42, "cancelled": False})
                    else:
                        self._json(
                            {
                                "id": 42,
                                "cancelled": False,
                                "executable": {
                                    "number": 7,
                                    "url": (
                                        f"{fake.base_url}/job/pipeline-integration/7/"
                                    ),
                                },
                            }
                        )
                    return
                if split.path == "/job/pipeline-integration/7/api/json":
                    fake.build_polls += 1
                    building = not fake.cancelled and (
                        fake.mode == "build_deadline" or fake.build_polls == 1
                    )
                    result = (
                        "ABORTED"
                        if fake.cancelled
                        else (None if building else fake.jenkins_result)
                    )
                    self._json(
                        {
                            "number": 7,
                            "queueId": 42,
                            "building": building,
                            "result": result,
                            "url": (f"{fake.base_url}/job/pipeline-integration/7/"),
                            "actions": [
                                {
                                    "parameters": [
                                        {"name": name, "value": values[0]}
                                        for name, values in sorted(
                                            fake.submitted_parameters.items()
                                        )
                                    ]
                                }
                            ],
                            "artifacts": [
                                {"fileName": Path(path).name, "relativePath": path}
                                for path in sorted(fake.artifacts)
                            ]
                            if not building
                            else [],
                        }
                    )
                    return
                artifact_prefix = "/job/pipeline-integration/7/artifact/"
                if split.path.startswith(artifact_prefix):
                    relative_path = split.path.removeprefix(artifact_prefix)
                    if (
                        fake.mode == "retrieval_deadline"
                        and relative_path == "pipeline-integration-result.json"
                    ):
                        time.sleep(1.2)
                    if (
                        fake.mode == "missing_download"
                        and relative_path != "pipeline-integration-result.json"
                    ):
                        self.send_error(404)
                        return
                    content = fake.artifacts.get(relative_path)
                    if content is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    if (
                        fake.mode == "http_stream_deadline"
                        and relative_path == "pipeline-integration-result.json"
                    ):
                        chunk_size = max(1, len(content) // 10)
                        try:
                            for offset in range(0, len(content), chunk_size):
                                _ = self.wfile.write(
                                    content[offset : offset + chunk_size]
                                )
                                self.wfile.flush()
                                time.sleep(0.15)
                        except BrokenPipeError:
                            pass
                        return
                    _ = self.wfile.write(content)
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                self._record()
                split = urlsplit(self.path)
                if split.path == "/queue/cancelItem":
                    fake.cancel_requests.append(self.path)
                    fake.cancelled = True
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if split.path == "/job/pipeline-integration/7/stop":
                    fake.cancel_requests.append(self.path)
                    fake.cancelled = True
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if split.path != "/job/pipeline-integration/buildWithParameters":
                    self.send_error(404)
                    return
                length = int(self.headers["Content-Length"])
                fake.submitted_parameters = parse_qs(
                    self.rfile.read(length).decode("ascii"),
                    strict_parsing=True,
                )
                if fake.mode == "submission_deadline":
                    time.sleep(1.2)
                self.send_response(201)
                if fake.mode != "indeterminate_submission":
                    self.send_header("Location", f"{fake.base_url}/queue/item/42/")
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler


@contextmanager
def _serve(fake: FakeJenkins) -> Generator[None, None, None]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    fake.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _handoff(  # noqa: PLR0913, PLR0917
    repository: Path,
    commit_sha: str,
    execution_id: str,
    output_root: Path,
    fake: FakeJenkins,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "SDI_JENKINS_BASE_URL": fake.base_url,
        "SDI_JENKINS_JOB_PATH": "pipeline-integration",
        "SDI_JENKINS_REPOSITORY": "github.com/example/sdi-fixture",
        "SDI_JENKINS_USERNAME": "github-handoff",
        "SDI_JENKINS_API_TOKEN": "test-api-token",
        "SDI_HANDOFF_POLL_INTERVAL_SECONDS": "0.01",
    }
    environment.update(environment_overrides or {})
    return subprocess.run(
        [
            "sdi-integration",
            "handoff-jenkins",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            RUN_REQUEST_PATH,
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--bundle-root",
            str(output_root / "bundle"),
            "--receipt-path",
            str(output_root / "handoff-receipt.json"),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def test_handoff_correlates_the_queue_and_retrieves_the_exact_build(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    execution_id, artifacts = _valid_bundle(
        repository, commit_sha, tmp_path / "jenkins-bundle"
    )
    fake = FakeJenkins(artifacts)

    with _serve(fake):
        completed = _handoff(
            repository,
            commit_sha,
            execution_id,
            tmp_path / "returned",
            fake,
        )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["execution_id"] == execution_id
    receipt = cast(
        "dict[str, object]",
        json.loads((tmp_path / "returned/handoff-receipt.json").read_text()),
    )
    assert receipt["schema_version"] == "sdi.github-jenkins-handoff-receipt/v1"
    assert receipt["phase"] == "completed"
    assert receipt["queue"] == {
        "queue_id": 42,
        "relative_url": "queue/item/42/",
    }
    assert receipt["build"] == {
        "build_number": 7,
        "relative_url": "job/pipeline-integration/7/",
    }
    terminal = cast("dict[str, object]", receipt["terminal"])
    assert terminal["outcome"] == "succeeded"
    transitions = cast("list[dict[str, object]]", receipt["transitions"])
    assert [item["phase"] for item in transitions] == [
        "validating",
        "preflighting",
        "submitting",
        "queued",
        "running",
        "retrieving",
        "completed",
    ]
    returned_files = {
        path.relative_to(tmp_path / "returned/bundle").as_posix(): path.read_bytes()
        for path in (tmp_path / "returned/bundle").rglob("*")
        if path.is_file()
    }
    assert returned_files == artifacts
    assert fake.queue_polls == 2
    assert fake.build_polls == 2
    assert fake.submitted_parameters == {
        "EXECUTION_ID": [execution_id],
        "GITHUB_RUN_ATTEMPT": ["1"],
        "GITHUB_RUN_ID": ["1000"],
        "HANDOFF_CONTRACT_VERSION": ["sdi.github-jenkins-handoff/v1"],
        "REQUESTED_GIT_REF": ["refs/heads/main"],
        "RESOLVED_COMMIT_SHA": [commit_sha],
        "RUN_REQUEST_PATH": [RUN_REQUEST_PATH],
    }
    expected_auth = (
        "Basic " + base64.b64encode(b"github-handoff:test-api-token").decode()
    )
    assert all(authorization == expected_auth for _, _, authorization in fake.requests)
    assert all("lastBuild" not in path for _, path, _ in fake.requests)
    assert "test-api-token" not in completed.stdout
    assert "test-api-token" not in completed.stderr
    assert fake.base_url not in completed.stdout
    assert fake.base_url not in completed.stderr


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("reachability", "reachability"),
        ("authentication", "authentication"),
        ("identity_mismatch", "authentication"),
        ("authorization", "authorization"),
        ("indeterminate_submission", "indeterminate_submission"),
        ("queue_missing", "queue_unavailable"),
    ],
)
def test_handoff_fails_once_with_typed_preflight_or_submission_fact(
    tmp_path: Path,
    mode: str,
    code: str,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    execution_id, artifacts = _valid_bundle(
        repository, commit_sha, tmp_path / "jenkins-bundle"
    )
    fake = FakeJenkins(artifacts, mode=mode)

    with _serve(fake):
        completed = _handoff(
            repository,
            commit_sha,
            execution_id,
            tmp_path / "returned",
            fake,
        )

    assert completed.returncode == 1
    receipt = cast(
        "dict[str, object]",
        json.loads((tmp_path / "returned/handoff-receipt.json").read_text()),
    )
    terminal = cast("dict[str, object]", receipt["terminal"])
    assert terminal["code"] == code
    trigger_requests = [
        path for method, path, _authorization in fake.requests if method == "POST"
    ]
    assert len(trigger_requests) == (
        1 if mode in {"indeterminate_submission", "queue_missing"} else 0
    )
    assert "test-api-token" not in completed.stderr
    assert fake.base_url not in completed.stderr


@pytest.mark.parametrize(
    ("mode", "environment_overrides", "code", "phase"),
    [
        (
            "http_deadline",
            {
                "SDI_HANDOFF_HTTP_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_QUEUE_WAIT_LIMIT_SECONDS": "5",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "10",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "11",
            },
            "http_operation",
            "queued",
        ),
        (
            "http_stream_deadline",
            {
                "SDI_HANDOFF_HTTP_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_RETRIEVAL_LIMIT_SECONDS": "5",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "10",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "11",
            },
            "http_operation",
            "retrieving",
        ),
        (
            "http_header_deadline",
            {
                "SDI_HANDOFF_HTTP_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_QUEUE_WAIT_LIMIT_SECONDS": "5",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "10",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "11",
            },
            "http_operation",
            "queued",
        ),
        (
            "submission_deadline",
            {
                "SDI_HANDOFF_HTTP_LIMIT_SECONDS": "5",
                "SDI_HANDOFF_SUBMISSION_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "10",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "11",
            },
            "submission_deadline",
            "submitting",
        ),
        (
            "queue_deadline",
            {
                "SDI_HANDOFF_QUEUE_WAIT_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "10",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "11",
            },
            "queue_deadline",
            "queued",
        ),
        (
            "build_deadline",
            {
                "SDI_HANDOFF_JENKINS_EXECUTION_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "10",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "11",
            },
            "build_deadline",
            "running",
        ),
        (
            "retrieval_deadline",
            {
                "SDI_HANDOFF_HTTP_LIMIT_SECONDS": "5",
                "SDI_HANDOFF_RETRIEVAL_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "10",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "11",
            },
            "retrieval_deadline",
            "retrieving",
        ),
        (
            "queue_deadline",
            {
                "SDI_HANDOFF_QUEUE_WAIT_LIMIT_SECONDS": "5",
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "1",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "2",
            },
            "outer_deadline",
            "queued",
        ),
    ],
)
def test_handoff_records_each_phase_deadline(
    tmp_path: Path,
    mode: str,
    environment_overrides: dict[str, str],
    code: str,
    phase: str,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    execution_id, artifacts = _valid_bundle(
        repository, commit_sha, tmp_path / "jenkins-bundle"
    )
    fake = FakeJenkins(artifacts, mode=mode)

    with _serve(fake):
        completed = _handoff(
            repository,
            commit_sha,
            execution_id,
            tmp_path / "returned",
            fake,
            environment_overrides,
        )

    assert completed.returncode == 1
    receipt = cast(
        "dict[str, object]",
        json.loads((tmp_path / "returned/handoff-receipt.json").read_text()),
    )
    terminal = cast("dict[str, object]", receipt["terminal"])
    assert terminal["code"] == code
    assert terminal["phase"] == phase
    assert terminal["limit_seconds"] == 1
    assert cast("float", terminal["elapsed_seconds"]) >= 1
    assert not (tmp_path / "returned/bundle").exists()


def test_github_job_safety_limit_must_exceed_the_runner_outer_limit(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    execution_id, artifacts = _valid_bundle(
        repository, commit_sha, tmp_path / "jenkins-bundle"
    )
    fake = FakeJenkins(artifacts)

    with _serve(fake):
        completed = _handoff(
            repository,
            commit_sha,
            execution_id,
            tmp_path / "returned",
            fake,
            {
                "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS": "2",
                "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS": "1",
            },
        )

    assert completed.returncode == 2
    assert fake.requests == []
    receipt = cast(
        "dict[str, object]",
        json.loads((tmp_path / "returned/handoff-receipt.json").read_text()),
    )
    terminal = cast("dict[str, object]", receipt["terminal"])
    assert terminal["code"] == "configuration"


@pytest.mark.parametrize(
    ("jenkins_result", "code"),
    [("FAILURE", "jenkins_failure"), ("ABORTED", "jenkins_aborted")],
)
def test_handoff_preserves_typed_terminal_jenkins_failure(
    tmp_path: Path,
    jenkins_result: str,
    code: str,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    execution_id, artifacts = _valid_bundle(
        repository, commit_sha, tmp_path / "jenkins-bundle"
    )
    fake = FakeJenkins(artifacts, jenkins_result=jenkins_result)

    with _serve(fake):
        completed = _handoff(
            repository,
            commit_sha,
            execution_id,
            tmp_path / "returned",
            fake,
        )

    assert completed.returncode == 1
    receipt = cast(
        "dict[str, object]",
        json.loads((tmp_path / "returned/handoff-receipt.json").read_text()),
    )
    terminal = cast("dict[str, object]", receipt["terminal"])
    assert terminal["code"] == code
    assert terminal["jenkins_result"] == jenkins_result
    assert (tmp_path / "returned/bundle").exists() is (jenkins_result == "FAILURE")


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing_result", "missing_artifact"),
        ("missing_download", "missing_artifact"),
        ("malformed_result", "invalid_artifact"),
        ("undeclared_file", "invalid_artifact"),
        ("digest_mismatch", "invalid_artifact"),
        ("size_mismatch", "invalid_artifact"),
        ("unsafe_path", "invalid_artifact"),
        ("identity_mismatch", "identity_mismatch"),
        ("returned_run_mismatch", "identity_mismatch"),
    ],
)
def test_handoff_rejects_incomplete_or_invalid_exact_build_artifacts(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    execution_id, original_artifacts = _valid_bundle(
        repository, commit_sha, tmp_path / "jenkins-bundle"
    )
    artifacts = dict(original_artifacts)
    mode = "success"
    submitted_execution_id = execution_id
    if mutation == "missing_result":
        del artifacts["pipeline-integration-result.json"]
    elif mutation == "missing_download":
        mode = "missing_download"
    elif mutation == "malformed_result":
        artifacts["pipeline-integration-result.json"] = b"{}\n"
    elif mutation == "undeclared_file":
        artifacts["undeclared.txt"] = b"not accepted\n"
    elif mutation == "digest_mismatch":
        path = next(path for path in artifacts if path.endswith("diagnostic.txt"))
        artifacts[path] = b"x" * len(artifacts[path])
    elif mutation == "size_mismatch":
        path = next(path for path in artifacts if path.endswith("diagnostic.txt"))
        artifacts[path] = artifacts[path] + b"x"
    elif mutation == "unsafe_path":
        artifacts["../outside"] = b"not accepted\n"
    elif mutation == "returned_run_mismatch":
        result = json.loads(artifacts["pipeline-integration-result.json"])
        result["repository"] = "github.com/example/another-repository"
        artifacts["pipeline-integration-result.json"] = (
            json.dumps(result, separators=(",", ":")).encode() + b"\n"
        )
    else:
        submitted_execution_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    fake = FakeJenkins(artifacts, mode=mode)

    with _serve(fake):
        completed = _handoff(
            repository,
            commit_sha,
            submitted_execution_id,
            tmp_path / "returned",
            fake,
        )

    assert completed.returncode == 1
    receipt = cast(
        "dict[str, object]",
        json.loads((tmp_path / "returned/handoff-receipt.json").read_text()),
    )
    terminal = cast("dict[str, object]", receipt["terminal"])
    assert terminal["code"] == code
    assert not (tmp_path / "returned/bundle").exists()


@pytest.mark.parametrize(
    ("mode", "expected_path"),
    [
        ("queue_deadline", "/queue/cancelItem?id=42"),
        ("build_deadline", "/job/pipeline-integration/7/stop"),
    ],
)
def test_cancellation_targets_only_the_correlated_queue_item_or_build(
    tmp_path: Path,
    mode: str,
    expected_path: str,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    execution_id, artifacts = _valid_bundle(
        repository, commit_sha, tmp_path / "jenkins-bundle"
    )
    fake = FakeJenkins(artifacts, mode=mode)
    output_root = tmp_path / "returned"
    environment = os.environ | {
        "SDI_JENKINS_BASE_URL": "placeholder",
        "SDI_JENKINS_JOB_PATH": "pipeline-integration",
        "SDI_JENKINS_REPOSITORY": "github.com/example/sdi-fixture",
        "SDI_JENKINS_USERNAME": "github-handoff",
        "SDI_JENKINS_API_TOKEN": "test-api-token",
        "SDI_HANDOFF_POLL_INTERVAL_SECONDS": "0.01",
    }

    with _serve(fake):
        environment["SDI_JENKINS_BASE_URL"] = fake.base_url
        process = subprocess.Popen(
            [
                "sdi-integration",
                "handoff-jenkins",
                "--repository",
                str(repository),
                "--requested-ref",
                "refs/heads/main",
                "--resolved-commit",
                commit_sha,
                "--run-request-path",
                RUN_REQUEST_PATH,
                "--execution-id",
                execution_id,
                "--github-run-id",
                "1000",
                "--github-run-attempt",
                "1",
                "--bundle-root",
                str(output_root / "bundle"),
                "--receipt-path",
                str(output_root / "handoff-receipt.json"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (mode == "queue_deadline" and fake.queue_polls > 0) or (
                mode == "build_deadline" and fake.build_polls > 0
            ):
                break
            time.sleep(0.01)
        else:
            process.kill()
            pytest.fail("handoff did not reach the cancellable Jenkins phase")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 128 + signal.SIGTERM
    assert fake.cancel_requests == [expected_path]
    receipt = cast(
        "dict[str, object]",
        json.loads((output_root / "handoff-receipt.json").read_text()),
    )
    terminal = cast("dict[str, object]", receipt["terminal"])
    assert receipt["phase"] == "cancelled"
    expected_terminal: dict[str, object] = {
        "observed_at": terminal["observed_at"],
        "outcome": "cancelled",
    }
    if mode == "build_deadline":
        expected_terminal["jenkins_result"] = "ABORTED"
    assert terminal == expected_terminal
    assert "test-api-token" not in stdout
    assert "test-api-token" not in stderr

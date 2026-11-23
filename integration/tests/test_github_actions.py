"""Black-box checks for the protected GitHub Actions presentation surface."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from sdi_pipeline_integration._bundle import validate_bundle_documents
from sdi_pipeline_integration._domain_contracts import (
    DeploymentSchema,
    ImageBuildResult,
)
from sdi_pipeline_integration._github_actions import render_image_build_section
from sdi_pipeline_integration._stage_registry import RUN_LIMITS
from tests._fixture_inputs import (
    FIXTURE_COMPOSITION_CONFIG,
    RED_RUN_EXIT,
    STAGE_DESCRIPTORS,
    SUPPLIED_INPUT_ARGUMENTS,
    write_fixture_composition_descriptor,
)

SOURCE_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = SOURCE_ROOT / ".github/workflows/pipeline-integration.yml"
BAKED_VENV = "/opt/sdi/integration/.venv"
BAKED_CLI = f"{BAKED_VENV}/bin/sdi-integration"
RUN_REQUEST_PATH = "runs/s-04/s-04-tc-03-c-05-fixture.yaml"
COMMITTED_FILES = (
    RUN_REQUEST_PATH,
    "runs/conformance/composition-negative-domain.yaml",
    "requirements/s-04/deliver-book-to-joe.md",
    "profiles/s-04/waffle-native-arm64.yaml",
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
    write_fixture_composition_descriptor(repository)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add GitHub presentation Fixture inputs")
    return repository, _git(repository, "rev-parse", "HEAD")


def _bundle(
    tmp_path: Path,
    run_request_path: str = RUN_REQUEST_PATH,
    exit_code: int = RED_RUN_EXIT,
) -> tuple[Path, dict[str, object]]:
    repository, commit_sha = _repository(tmp_path)
    bundle_root = tmp_path / "bundle"
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
            run_request_path,
            "--bundle-root",
            str(bundle_root),
            *SUPPLIED_INPUT_ARGUMENTS,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == exit_code, completed.stderr
    return bundle_root, json.loads(completed.stdout)


def _image_build_section(summary: str) -> str:
    return summary.split("## Image build\n", 1)[1].split("\n## ", maxsplit=1)[0]


def _write_completed_receipt(path: Path, execution_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "sdi.github-jenkins-handoff-receipt/v1",
                "execution_id": execution_id,
                "github": {"run_id": 1000, "run_attempt": 1},
                "phase": "completed",
                "transitions": [
                    {"phase": phase, "observed_at": f"2026-09-11T00:00:0{index}Z"}
                    for index, phase in enumerate(
                        (
                            "validating",
                            "preflighting",
                            "submitting",
                            "queued",
                            "running",
                            "retrieving",
                            "completed",
                        )
                    )
                ],
                "queue": {"queue_id": 42, "relative_url": "queue/item/42/"},
                "build": {
                    "build_number": 7,
                    "relative_url": "job/pipeline-integration/7/",
                },
                "terminal": {
                    "outcome": "succeeded",
                    "observed_at": "2026-09-11T00:00:06Z",
                    "jenkins_result": "SUCCESS",
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _write_failed_receipt(path: Path, execution_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "sdi.github-jenkins-handoff-receipt/v1",
                "execution_id": execution_id,
                "github": {"run_id": 1000, "run_attempt": 1},
                "phase": "failed",
                "transitions": [
                    {"phase": phase, "observed_at": f"2026-09-11T00:00:0{index}Z"}
                    for index, phase in enumerate(
                        (
                            "validating",
                            "preflighting",
                            "submitting",
                            "queued",
                            "running",
                            "retrieving",
                            "failed",
                        )
                    )
                ],
                "queue": {"queue_id": 42, "relative_url": "queue/item/42/"},
                "build": {
                    "build_number": 7,
                    "relative_url": "job/pipeline-integration/7/",
                },
                "terminal": {
                    "outcome": "failed",
                    "observed_at": "2026-09-11T00:00:06Z",
                    "code": "jenkins_failure",
                    "phase": "retrieving",
                    "jenkins_result": "FAILURE",
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _write_cancelled_receipt(path: Path, execution_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "sdi.github-jenkins-handoff-receipt/v1",
                "execution_id": execution_id,
                "github": {"run_id": 1000, "run_attempt": 1},
                "phase": "cancelled",
                "transitions": [
                    {
                        "phase": "validating",
                        "observed_at": "2026-09-11T00:00:00Z",
                    },
                    {
                        "phase": "cancelled",
                        "observed_at": "2026-09-11T00:00:01Z",
                    },
                ],
                "terminal": {
                    "outcome": "cancelled",
                    "observed_at": "2026-09-11T00:00:01Z",
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _write_preflight_failure_receipt(path: Path, execution_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "sdi.github-jenkins-handoff-receipt/v1",
                "execution_id": execution_id,
                "github": {"run_id": 1000, "run_attempt": 1},
                "phase": "failed",
                "transitions": [
                    {
                        "phase": "validating",
                        "observed_at": "2026-09-11T00:00:00Z",
                    },
                    {
                        "phase": "preflighting",
                        "observed_at": "2026-09-11T00:00:01Z",
                    },
                    {
                        "phase": "failed",
                        "observed_at": "2026-09-11T00:00:02Z",
                    },
                ],
                "terminal": {
                    "outcome": "failed",
                    "observed_at": "2026-09-11T00:00:02Z",
                    "code": "reachability",
                    "phase": "preflighting",
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def test_completed_published_bundle_renders_status_first_summary(
    tmp_path: Path,
) -> None:
    bundle_root, result = _bundle(tmp_path)
    execution_id = str(result["execution_id"])
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_completed_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(bundle_root),
            "--handoff-outcome",
            "success",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
            "--artifact-url",
            "https://github.com/example/sdi-fixture/actions/runs/1000/artifacts/2000",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("# Pipeline integration: succeeded\n")
    assert f"`{execution_id}`" in completed.stdout
    assert "| Stage | Lifecycle | Execution | Implementation | Domain outcome |" in (
        completed.stdout
    )
    assert "`bundle/pipeline-integration-result.json`" in completed.stdout
    assert "`handoff-receipt.json`" in completed.stdout
    assert "- Combination: `C-05`" in completed.stdout
    assert "Supported combination" not in completed.stdout
    assert "KPI evaluation: `not evaluated`" in completed.stdout
    assert "Fixture warning" in completed.stdout
    assert "aggregate Domain verdict" not in completed.stdout
    image_build = _image_build_section(completed.stdout)
    assert (
        "| Service | Artifact | Host | Architectures | Status | Short digest | Reason |"
    ) in image_build
    for service_id in (
        "fixture-localization",
        "fixture-navigation",
        "fixture-mission-control",
    ):
        assert (
            f"| `{service_id}` | - | - | - | `not_evaluated` | - | - |" in image_build
        )
    assert "`orin`" not in image_build
    assert "stack-local pipeline registry" in image_build


@pytest.mark.parametrize(
    "artifact_url",
    [
        "https://10.0.0.8/private/artifact",
        "https://github.com/example/sdi-fixture/actions/runs/999/artifacts/2000",
        "https://user:token@github.com/example/sdi-fixture/actions/runs/1000/artifacts/2000",
    ],
)
def test_summary_rejects_private_or_mismatched_artifact_urls(
    tmp_path: Path, artifact_url: str
) -> None:
    bundle_root, result = _bundle(tmp_path)
    execution_id = str(result["execution_id"])
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_completed_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(bundle_root),
            "--handoff-outcome",
            "success",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
            "--artifact-url",
            artifact_url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""


def test_artifact_publication_failure_keeps_valid_result_red(tmp_path: Path) -> None:
    bundle_root, result = _bundle(tmp_path)
    execution_id = str(result["execution_id"])
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_completed_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(bundle_root),
            "--handoff-outcome",
            "success",
            "--publication-outcome",
            "failure",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith("# Pipeline integration: failed\n")
    assert "Publication: `failure`" in completed.stdout


def test_negative_domain_result_remains_a_successful_pipeline_integration(
    tmp_path: Path,
) -> None:
    bundle_root, result = _bundle(
        tmp_path, "runs/conformance/composition-negative-domain.yaml", exit_code=0
    )
    execution_id = str(result["execution_id"])
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_completed_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(bundle_root),
            "--handoff-outcome",
            "success",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("# Pipeline integration: succeeded\n")
    assert "`failed`" in completed.stdout
    assert "`sdi.domain.requirement-unsatisfied`" in completed.stdout
    image_build = _image_build_section(completed.stdout)
    assert "| Service |" not in image_build
    assert "stack-local pipeline registry" not in image_build
    assert re.search(
        r"^Image build `skipped`: `sdi\.dependency\.[a-z-]+`: \S",
        image_build,
        re.MULTILINE,
    ), image_build


def test_structured_jenkins_failure_remains_red_after_bundle_publication(
    tmp_path: Path,
) -> None:
    bundle_root, result = _bundle(tmp_path)
    execution_id = str(result["execution_id"])
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_failed_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(bundle_root),
            "--handoff-outcome",
            "failure",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith("# Pipeline integration: failed\n")
    assert "Failure code: `jenkins_failure`" in completed.stdout
    assert "Jenkins result: `FAILURE`" in completed.stdout
    assert "## Stage facts" in completed.stdout


def test_resultless_cancellation_does_not_promise_a_pipeline_result(
    tmp_path: Path,
) -> None:
    execution_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_cancelled_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(tmp_path / "absent-bundle"),
            "--handoff-outcome",
            "cancelled",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
            "--workflow-cancelled",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("# Pipeline integration: cancelled\n")
    assert "No Pipeline integration result is promised for cancellation." in (
        completed.stdout
    )
    assert "## Stage facts" not in completed.stdout


def test_workflow_is_one_protected_main_only_handoff_job() -> None:
    workflow = YAML(typ="safe").load(  # pyright: ignore[reportUnknownMemberType]
        WORKFLOW_PATH.read_text(encoding="utf-8")
    )

    assert workflow["name"] == "Pipeline integration"
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs == {
        "run_request_path": {
            "description": "Committed Pipeline integration run-request path",
            "required": True,
            "type": "string",
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"handoff"}

    job = workflow["jobs"]["handoff"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "pipeline-integration-jenkins"
    assert job["runs-on"] == "sdi-jenkins-handoff"
    assert job["timeout-minutes"] * 60 == RUN_LIMITS.github_job_seconds

    steps = job["steps"]
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", step["uses"])
        for step in steps
        if "uses" in step
    )
    assert [step["name"] for step in steps] == [
        "Check out immutable protected revision",
        "Confirm runner integration code matches the revision",
        "Validate input and assign Execution ID",
        "Handoff to Jenkins",
        "Publish accepted evidence",
        "Render final summary",
    ]
    assert steps[0]["uses"] == (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    )
    assert steps[0]["with"] == {
        "ref": "${{ github.sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
        "filter": "blob:none",
    }
    identify_run = steps[2]["run"]
    handoff_run = steps[3]["run"]
    assert "identify-run" in identify_run
    assert "handoff-jenkins" not in identify_run
    assert handoff_run.count("handoff-jenkins") == 1
    assert f"exec {BAKED_CLI} handoff-jenkins" in handoff_run
    assert '> "$HANDOFF_RESULT"' in handoff_run
    assert all(
        operation not in handoff_run.casefold()
        for operation in ("queue/item", "lastbuild", "buildwithparameters", "curl ")
    )
    assert "env" not in job
    assert (
        steps[3]["env"]["SDI_JENKINS_API_TOKEN"]
        == "${{ secrets.SDI_JENKINS_API_TOKEN }}"  # noqa: S105
    )
    assert steps[3]["env"]["SDI_JENKINS_BASE_URL"] == "http://controller:8080"

    publication = steps[4]
    assert publication["id"] == "publication"
    assert publication["if"] == "always() && steps.identify.outcome == 'success'"
    assert publication["uses"] == (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )
    assert publication["with"]["name"] == (
        "pipeline-integration-${{ steps.identify.outputs.execution_id }}"
    )
    assert publication["with"]["retention-days"] == 90
    assert publication["with"]["if-no-files-found"] == "error"
    assert publication["with"]["path"].splitlines() == [
        "${{ steps.identify.outputs.output_root }}/bundle",
        "${{ steps.identify.outputs.output_root }}/handoff-receipt.json",
    ]

    summary = steps[5]
    assert summary["if"] == "always() && steps.identify.outcome == 'success'"
    assert "github-summary" in summary["run"]
    assert '--github-run-id "$GITHUB_RUN_ID"' in summary["run"]
    assert '--github-run-attempt "$GITHUB_RUN_ATTEMPT"' in summary["run"]
    assert summary["env"]["JOB_STATUS"] == "${{ job.status }}"
    assert "cancelled()" not in summary["env"].values()
    assert '[ "$JOB_STATUS" = cancelled ]' in summary["run"]
    assert "$GITHUB_STEP_SUMMARY" in summary["run"]


def test_workflow_runs_the_runner_images_checked_integration_environment() -> None:
    workflow = YAML(typ="safe").load(  # pyright: ignore[reportUnknownMemberType]
        WORKFLOW_PATH.read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["handoff"]["steps"]

    digest_run = steps[1]["run"]
    assert "integration-code-digest --root integration" in digest_run
    assert "/etc/sdi/integration-code-digest" in digest_run
    assert "exit 1" in digest_run
    run_steps = [step for step in steps if "run" in step]
    assert all(step.get("shell") == "bash" for step in run_steps)
    for step in run_steps:
        assert "uv " not in step["run"]
        assert "integration/.venv" not in step["run"].replace(BAKED_VENV, "")
        assert not any(name.startswith(("UV_", "ZIG_")) for name in step.get("env", {}))
    assert all(BAKED_CLI in step["run"] for step in run_steps)


def test_invalid_workflow_input_fails_before_any_jenkins_contact(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _repository(tmp_path)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0)
    port = listener.getsockname()[1]
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
            "runs/s-04/not-reviewed.yaml",
        ],
        check=False,
        capture_output=True,
        env=os.environ
        | {
            "SDI_JENKINS_BASE_URL": f"http://127.0.0.1:{port}",
            "SDI_JENKINS_JOB_PATH": "pipeline-integration",
            "SDI_JENKINS_REPOSITORY": "github.com/example/sdi-fixture",
            "SDI_JENKINS_USERNAME": "github-handoff",
            "SDI_JENKINS_API_TOKEN": "not-contacted",
        },
        text=True,
    )
    try:
        with pytest.raises(BlockingIOError):
            listener.accept()
    finally:
        listener.close()

    assert completed.returncode == 2
    assert completed.stdout == ""


def test_resultless_handoff_failure_renders_its_published_receipt(
    tmp_path: Path,
) -> None:
    execution_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_preflight_failure_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "1000",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(tmp_path / "absent-bundle"),
            "--handoff-outcome",
            "failure",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith("# Pipeline integration: failed\n")
    assert "Failure code: `reachability`" in completed.stdout
    assert "Result: `not returned`" in completed.stdout
    assert "## Stage facts" not in completed.stdout
    assert "`handoff-receipt.json`" in completed.stdout


def test_summary_rejects_receipt_from_a_different_github_run(tmp_path: Path) -> None:
    bundle_root, result = _bundle(tmp_path)
    execution_id = str(result["execution_id"])
    receipt_path = tmp_path / "handoff-receipt.json"
    _write_completed_receipt(receipt_path, execution_id)

    completed = subprocess.run(
        [
            "sdi-integration",
            "github-summary",
            "--execution-id",
            execution_id,
            "--github-run-id",
            "999",
            "--github-run-attempt",
            "1",
            "--receipt-path",
            str(receipt_path),
            "--bundle-root",
            str(bundle_root),
            "--handoff-outcome",
            "success",
            "--publication-outcome",
            "success",
            "--artifact-name",
            f"pipeline-integration-{execution_id}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith("# Pipeline integration: failed\n")
    assert "Receipt: `identity mismatch`" in completed.stdout


@pytest.mark.parametrize(
    ("failure_run_id", "expected_count", "expected_returncode"),
    [(None, 6, 0), (1001, 2, 1)],
)
def test_s04_helper_prints_exact_urls_waits_sequentially_and_stops_on_failure(
    tmp_path: Path,
    failure_run_id: int | None,
    expected_count: int,
    expected_returncode: int,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log_path = Path(os.environ["FAKE_GH_LOG"])
entries = json.loads(log_path.read_text()) if log_path.exists() else []
arguments = sys.argv[1:]
if arguments[0] == "api":
    request = json.load(sys.stdin)
    run_id = 1000 + sum(entry["operation"] == "dispatch" for entry in entries)
    entries.append({"operation": "dispatch", "request": request})
    log_path.write_text(json.dumps(entries))
    print(json.dumps({
        "workflow_run_id": run_id,
        "html_url": f"https://github.com/example/repository/actions/runs/{run_id}",
    }))
    raise SystemExit(0)
if arguments[:2] == ["run", "watch"]:
    run_id = int(arguments[2])
    entries.append({"operation": "watch", "run_id": run_id})
    log_path.write_text(json.dumps(entries))
    failure_run_id = os.environ.get("FAKE_FAILURE_RUN_ID")
    raise SystemExit(1 if failure_run_id == str(run_id) else 0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    log_path = tmp_path / "gh-log.json"

    completed = subprocess.run(
        [
            "sdi-integration",
            "dispatch-s-04",
            "--repository",
            "example/repository",
        ],
        check=False,
        capture_output=True,
        env=os.environ
        | ({"FAKE_FAILURE_RUN_ID": str(failure_run_id)} if failure_run_id else {})
        | {
            "FAKE_GH_LOG": str(log_path),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )

    assert completed.returncode == expected_returncode
    assert completed.stdout.splitlines() == [
        f"https://github.com/example/repository/actions/runs/{1000 + index}"
        for index in range(expected_count)
    ]
    entries = json.loads(log_path.read_text())
    assert [entry["operation"] for entry in entries] == [
        operation
        for _index in range(expected_count)
        for operation in ("dispatch", "watch")
    ]
    assert [
        entry["request"]["inputs"]["run_request_path"]
        for entry in entries
        if entry["operation"] == "dispatch"
    ] == [
        f"runs/s-04/s-04-tc-03-c-{index:02d}-fixture.yaml"
        for index in range(1, expected_count + 1)
    ]
    assert all(
        entry["request"].get("ref") == "main"
        for entry in entries
        if entry["operation"] == "dispatch"
    )


def test_image_build_table_joins_built_and_failed_records_with_placements(
    tmp_path: Path,
) -> None:
    bundle_root, _ = _bundle(tmp_path)
    result, documents = validate_bundle_documents(bundle_root)
    deployment = documents["deployment_schema"].model_dump(mode="json")
    deployment["evidence_basis"] = "implemented"
    deployment["placements"] = [
        {
            "service_id": "fixture-localization",
            "status": "resolved",
            "artifact_id": "localization-debs",
            "host": "orin",
        },
        {
            "service_id": "fixture-navigation",
            "status": "resolved",
            "artifact_id": "navigation-debs",
            "host": "pi",
        },
        {"service_id": "fixture-mission-control", "status": "unresolved"},
    ]
    image_build = documents["image_build_result"].model_dump(mode="json")
    image_build["evidence_basis"] = "implemented"
    image_build["images"] = [
        {
            "service_id": "fixture-localization",
            "status": "built",
            "repository": "sdi/fixture-localization",
            "digest": "sha256:" + "a1" * 32,
            "platforms": [
                {"architecture": "arm64", "digest": "sha256:" + "b2" * 32},
                {"architecture": "amd64", "digest": "sha256:" + "c3" * 32},
            ],
        },
        {
            "service_id": "fixture-navigation",
            "status": "failed",
            "reason": "build-failed",
            "detail": "E: apt `ros-jazzy-nav2` | <missing> & gone",
        },
        {
            "service_id": "fixture-mission-control",
            "status": "failed",
            "reason": "unresolved-placement",
            "detail": "no host satisfies the placement",
        },
    ]

    section = "\n".join(
        render_image_build_section(
            result,
            {
                "deployment_schema": DeploymentSchema.model_validate(deployment),
                "image_build_result": ImageBuildResult.model_validate(image_build),
            },
        )
    )

    assert (
        "| `fixture-localization` | `localization-debs` | `orin` | amd64, arm64 "
        "| `built` | `sha256:a1a1a1a1a1a1` | - |"
    ) in section
    assert (
        "| `fixture-navigation` | `navigation-debs` | `pi` | - | `failed` | - "
        "| `build-failed`: E: apt &#96;ros-jazzy-nav2&#96; \\| &lt;missing&gt; "
        "&amp; gone |"
    ) in section
    assert (
        "| `fixture-mission-control` | - | - | - | `failed` | - "
        "| `unresolved-placement`: no host satisfies the placement |"
    ) in section
    navigation_row = next(
        line for line in section.splitlines() if "fixture-navigation" in line
    )
    assert len(re.split(r"(?<!\\)\|", navigation_row)) == 9
    assert "stack-local pipeline registry `registry:5000`" in section

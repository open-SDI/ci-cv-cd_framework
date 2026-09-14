"""Black-box checks for the installed command-line interface."""

from __future__ import annotations

import json
import subprocess
from importlib.metadata import version
from pathlib import Path


def test_installed_cli_reports_distribution_version() -> None:
    completed = subprocess.run(
        ["sdi-integration", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )

    distribution_version = version("sdi-pipeline-integration")
    assert completed.stdout == f"sdi-integration {distribution_version}\n"


def test_installed_cli_exposes_help() -> None:
    completed = subprocess.run(
        ["sdi-integration", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.startswith("usage: sdi-integration")


def test_handoff_cli_exposes_only_non_secret_run_and_output_arguments() -> None:
    completed = subprocess.run(
        ["sdi-integration", "handoff-jenkins", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--github-run-id" in completed.stdout
    assert "--github-run-attempt" in completed.stdout
    assert "--bundle-root" in completed.stdout
    assert "--receipt-path" in completed.stdout
    assert "--token" not in completed.stdout
    assert "--username" not in completed.stdout
    assert "--jenkins-url" not in completed.stdout
    assert "--timeout" not in completed.stdout


def test_cli_reads_the_descriptor_selected_agent_image() -> None:
    descriptor = (
        Path(__file__).parents[2]
        / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    )
    completed = subprocess.run(
        ["sdi-integration", "adapter-image", "--descriptor", str(descriptor)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (
        "docker.io/library/python@"
        "sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2\n"
    )


def test_fixture_adapter_exposes_only_the_one_shot_run_operation() -> None:
    completed = subprocess.run(
        ["sdi-fixture-adapter", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "{run}" in completed.stdout
    assert "composition" not in completed.stdout


def test_cli_writes_and_freshness_checks_contract_schemas(tmp_path: Path) -> None:
    write = subprocess.run(
        [
            "sdi-integration",
            "schemas",
            "--write",
            "--directory",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert write.returncode == 0, write.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "accepted-attempt-envelope-v1.schema.json",
        "adapter-descriptor-v1.schema.json",
        "composition-blueprint-v1.schema.json",
        "deployment-result-v1.schema.json",
        "deployment-schema-v1.schema.json",
        "fixture-case-v1.schema.json",
        "handoff-receipt-v1.schema.json",
        "image-build-result-v1.schema.json",
        "mobility-requirements-specification-v1.schema.json",
        "pipeline-integration-result-v1.schema.json",
        "pipeline-integration-run-request-v1.schema.json",
        "recovery-manifest-v1.schema.json",
        "stage-adapter-request-v1.schema.json",
        "stage-adapter-response-v1.schema.json",
        "stage-profile-v1.schema.json",
        "target-execution-profile-v1.schema.json",
        "validation-evidence-v1.schema.json",
    ]

    check = subprocess.run(
        [
            "sdi-integration",
            "schemas",
            "--check",
            "--directory",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert check.returncode == 0, check.stderr

    run_schema = json.loads(
        (tmp_path / "pipeline-integration-run-request-v1.schema.json").read_text()
    )
    path_pattern = run_schema["properties"]["requirements_specification"]["pattern"]
    assert "[A-Za-z]:/" in path_pattern

    profile_schema_path = tmp_path / "target-execution-profile-v1.schema.json"
    profile_schema = json.loads(profile_schema_path.read_text())
    resources = profile_schema["$defs"]["ResourceCapacity"]
    assert resources["minProperties"] == 1
    assert profile_schema["$defs"]["NetworkLink"]["anyOf"] == [
        {"required": ["latency_ms"]},
        {"required": ["bandwidth_mbps"]},
    ]
    assert profile_schema["properties"]["network_links"]["minItems"] == 1
    profile_schema_text = profile_schema_path.read_text()
    assert '"type": "null"' not in profile_schema_text
    assert '"exclusiveMinimum": 0' in profile_schema_text
    assert '"minimum": 0' in profile_schema_text
    assert '"gt":' not in profile_schema_text
    assert '"ge":' not in profile_schema_text

    request_schema = json.loads(
        (tmp_path / "stage-adapter-request-v1.schema.json").read_text()
    )
    assert set(request_schema["properties"]) == {
        "schema_version",
        "correlation",
        "work_limit_seconds",
        "inputs",
        "outputs",
        "diagnostic",
    }
    forbidden_request_fields = {
        "jenkins_build",
        "secret",
        "private_endpoint",
        "implementation_mode",
        "final_result",
    }
    assert forbidden_request_fields.isdisjoint(request_schema["properties"])
    output_grant = request_schema["$defs"]["OutputGrant"]
    assert "required" in output_grant["required"]

    receipt_schema = json.loads(
        (tmp_path / "handoff-receipt-v1.schema.json").read_text()
    )
    assert set(receipt_schema["properties"]) == {
        "schema_version",
        "execution_id",
        "github",
        "phase",
        "transitions",
        "queue",
        "build",
        "terminal",
    }
    assert {
        "jenkins_base_url",
        "repository",
        "pipeline_integration_result",
        "token",
        "username",
    }.isdisjoint(receipt_schema["properties"])

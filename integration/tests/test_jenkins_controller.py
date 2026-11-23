"""Black-box checks for the repository-owned Jenkins controller deployment."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from ruamel.yaml import YAML

from sdi_pipeline_integration._handoff_contracts import (
    DEPLOYMENT_MANIFEST_PATH,
    HANDOFF_PARAMETERS,
)
from sdi_pipeline_integration._integration_code import (
    STALE_IMAGE_REASON,
    integration_code_digest,
)
from sdi_pipeline_integration._stage_registry import RUN_LIMITS, STAGE_ORDER

if TYPE_CHECKING:
    from collections.abc import Mapping


REPOSITORY_ROOT = Path(__file__).parents[2]
JENKINS_ROOT = REPOSITORY_ROOT / "deployment" / "jenkins"
EXPECTED_CONTROLLER_IMAGE = (
    "docker.io/jenkins/jenkins:2.568.3-jdk21@"
    "sha256:68964c38dbb70c0bf6adff480d6dda2e1191d2d07e076c9fd2996345303eb999"
)
FAKE_AGENT_IMAGES = (
    "docker.io/example/composition@sha256:" + "1" * 64,
    "docker.io/example/image-build@sha256:" + "2" * 64,
    "docker.io/example/cv@sha256:" + "3" * 64,
    "docker.io/example/cd@sha256:" + "4" * 64,
)
SMOKE_CHECK = JENKINS_ROOT / "bin" / "smoke-check"
STAGE_ARGUMENTS = " ".join(f"--stage {stage}" for stage in STAGE_ORDER)
JOB_REPOSITORY_URLS = {
    "/job/pipeline-integration/config.xml": "https://github.com/example/repository.git",
    "/job/pipeline-integration-fork/config.xml": (
        "https://github.com/example-fork/repository.git"
    ),
}


def _load_yaml(path: Path) -> Mapping[str, Any]:
    yaml = YAML(typ="safe")
    loaded = yaml.load(path)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(loaded, dict)
    return cast("Mapping[str, Any]", loaded)


def _write_fake_deployment_tools(fake_bin: Path) -> None:
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$DOCKER_LOG"\n'
        "printf 'images=%s|%s|%s|%s\\n' "
        '"${JENKINS_COMPOSITION_AGENT_IMAGE:-}" '
        '"${JENKINS_IMAGE_BUILD_AGENT_IMAGE:-}" '
        '"${JENKINS_CV_AGENT_IMAGE:-}" '
        '"${JENKINS_CD_AGENT_IMAGE:-}" >> "$DOCKER_LOG"\n'
        "printf 'network-internal=%s\\n' "
        '"${JENKINS_AGENT_NETWORK_INTERNAL:-}" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        "  'version --format {{.Server.Os}}/{{.Server.Arch}}') "
        "printf 'linux/amd64\\n' ;;\n"
        "  'version --format {{.Server.Version}}') printf '29.7.2\\n' ;;\n"
        "  'compose version --short') printf '5.5.1\\n' ;;\n"
        "  'volume inspect sdi-jenkins-'*) exit 1 ;;\n"
        "  'network inspect sdi-jenkins-'*) exit 1 ;;\n"
        "  'volume inspect --format '*' sdi-jenkins-'*) "
        "for probe do :; done; printf '%s\\n' \"${probe#sdi-jenkins-}\" ;;\n"
        "  'network create --label org.sdi.preflight='*) "
        'for argument do case "$argument" in org.sdi.preflight=*) '
        'printf \'%s\\n\' "${argument#*=}" > "$DOCKER_LOG.network-owner" ;; '
        "esac; done; "
        f"printf '%s\\n' '{'a' * 64}' ;;\n"
        "  'network inspect --format '*) cat \"$DOCKER_LOG.network-owner\" ;;\n"
        "  *' rm --force --stop --volumes k3s-'*) : > \"$DOCKER_LOG.k3s-gone\" ;;\n"
        "  *' ps --all --quiet') "
        "[ \"${FORCE_KILL:-}\" != 1 ] || printf 'forced-container\\n'; "
        '[ -z "${FAKE_K3S_UP:-}" ] || [ -e "$DOCKER_LOG.k3s-gone" ] || '
        "printf 'k3s-node\\n' ;;\n"
        "  'inspect --format {{.State.ExitCode}} forced-container') "
        "printf '137\\n' ;;\n"
        "  'inspect --format {{.State.ExitCode}} k3s-node') printf '137\\n' ;;\n"
        "  'image inspect --format {{json .Config.Volumes}}'*) "
        "printf '%s\\n' \"${FAKE_AGENT_VOLUMES:-null}\" ;;\n"
        "  'image inspect --format {{.Id}} '*) "
        '[ -z "${FAKE_NO_IMAGES:-}" ] || exit 1; shift 4; '
        'for image do printf \'sha256:%s-%s\\n\' "${FAKE_IMAGE_ID:-1}" "$image"; '
        "done ;;\n"
        "  *' -T runner-'*' show') exit 3 ;;\n"
        "  *' --entrypoint sdi-cv-warm cv') "
        '[ -z "${FAKE_WARM_FAIL:-}" ] || exit 1; '
        '[ -z "${FAKE_WARM_HANG:-}" ] || exec sleep 30 ;;\n'
        "  *' -T runner-'*' register '*) "
        "printf 'the registration token is empty or invalid\\n' >&2; exit 2 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)

    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf \'uv %s\\n\' "$*" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        f"  *'adapter-image --repository '*' {STAGE_ARGUMENTS}') "
        f"printf '%s\\n' {' '.join(FAKE_AGENT_IMAGES)} ;;\n"
        "  *sdi-integration*integration-code-digest*) printf '%064d\\n' 0 ;;\n"
        "  *sdi-integration*validate-installation*) exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)

    for name, output in (("curl", ""), ("systemctl", "systemd 259")):
        command = fake_bin / name
        command.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        command.chmod(0o755)
    git = fake_bin / "git"
    git.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  --version) printf '%s\\n' 'git version 2.53.0' ;;\n"
        "  *'status --porcelain=v1 --untracked-files=all') exit 0 ;;\n"
        "  *'stash create') exit 0 ;;\n"
        "  *'rev-parse --verify --quiet HEAD^{tree}') "
        "printf '%s\\n' \"${FAKE_TREE:-1}\" ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)


def _assert_plaintext_relocated_agent_url_is_rejected(
    config: Path, fake_bin: Path, docker_log: Path
) -> None:
    valid_config = config.read_text()
    config.write_text(
        valid_config.replace(
            "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080",
            "JENKINS_AGENT_CONTROLLER_URL=http://remote.example.test:8080",
        )
    )
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    config.write_text(valid_config)

    assert completed.returncode == 2
    assert "internal Compose URL or an uncredentialed HTTPS URL" in completed.stderr

    config.write_text(
        valid_config.replace(
            "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080",
            "JENKINS_AGENT_CONTROLLER_URL=https://jenkins.example.test",
        )
    )
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    config.write_text(valid_config)

    assert completed.returncode == 0, completed.stderr
    assert "network-internal=false" in docker_log.read_text()


def _assert_single_agent_reconciliation(
    config: Path,
    fake_bin: Path,
    docker_log: Path,
    unrelated_agent_secret: Path,
) -> None:
    class IdleJenkinsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/crumbIssuer/api/json":
                body = b'{"crumbRequestField":"Jenkins-Crumb","crumb":"test"}'
            elif self.path.startswith("/queue/api/json"):
                body = b'{"items":[]}'
            elif self.path.startswith(
                (
                    "/job/pipeline-integration/api/json",
                    "/job/pipeline-integration-fork/api/json",
                )
            ):
                body = b'{"builds":[]}'
            elif self.path.startswith("/api/json"):
                body = b'{"quietingDown":false}'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *arguments: object) -> None:  # noqa: A002
            del format, arguments

    docker_log.write_text("")
    unrelated_agent_secret.chmod(0o000)
    server = ThreadingHTTPServer(("127.0.0.1", 0), IdleJenkinsHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    original_config = config.read_text()
    config.write_text(
        original_config.replace(
            "JENKINS_HTTP_PORT=8080", f"JENKINS_HTTP_PORT={server.server_port}"
        )
    )
    try:
        for agent in ("ci", "image-build", "cv", "cd"):
            subprocess.run(
                [
                    JENKINS_ROOT / "bin" / "stack",
                    "--config",
                    config,
                    "reconcile-agent",
                    agent,
                ],
                check=True,
                capture_output=True,
                env={
                    "DOCKER_LOG": str(docker_log),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                },
                text=True,
            )
    finally:
        config.write_text(original_config)
        server.shutdown()
        thread.join()
        server.server_close()
    reconcile_log = docker_log.read_text()

    for agent in ("ci", "image-build", "cv", "cd"):
        assert f"build {agent}" in reconcile_log
        assert (
            f"up --no-build --no-deps --detach --wait --wait-timeout 240 {agent}"
            in reconcile_log
        )
    assert "--tag sdi-jenkins-controller" not in reconcile_log
    assert reconcile_log.count("image inspect --format {{json .Config.Volumes}}") == 4
    assert reconcile_log.count("--entrypoint sdi-cv-warm cv") == 1
    assert (
        reconcile_log.index("build cv")
        < reconcile_log.index("--entrypoint sdi-cv-warm cv")
        < reconcile_log.index(
            "up --no-build --no-deps --detach --wait --wait-timeout 240 cv"
        )
    )


def _assert_stack_lifecycle_log(lifecycle_log: str) -> None:
    assert "compose" in lifecycle_log
    assert "build integration ci image-build cv cd" in lifecycle_log
    assert "up --no-build --detach --wait --wait-timeout 240" in lifecycle_log
    assert "image inspect --format {{json .Config.Volumes}}" in lifecycle_log
    assert "down" in lifecycle_log
    assert "down --volumes" not in lifecycle_log


def test_controller_image_and_complete_plugin_set_are_exactly_pinned() -> None:
    dockerfile = (JENKINS_ROOT / "controller" / "Dockerfile").read_text()
    assert f"FROM {EXPECTED_CONTROLLER_IMAGE}" in dockerfile
    assert "LABEL org.sdi.project=ci-cv-cd_framework" in dockerfile
    assert "jenkins-plugin-cli --latest=false" in dockerfile

    plugin_lines = [
        line
        for line in (JENKINS_ROOT / "controller" / "plugins.txt")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
    ]
    assert len(plugin_lines) == 47
    assert len(plugin_lines) == len(set(plugin_lines))
    assert all(
        re.fullmatch(r"[a-z0-9-]+:[A-Za-z0-9_.+-]+", line) for line in plugin_lines
    )
    assert "configuration-as-code:2121.v86fe99d4b_b_a_b_" in plugin_lines
    assert "job-dsl:3732.v9a_c49a_61a_313" in plugin_lines
    assert "git:5.10.1" in plugin_lines
    assert "matrix-auth:3.3" in plugin_lines


def test_compose_exposes_only_loopback_http_and_persists_only_named_state() -> None:
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    controller = compose["services"]["controller"]

    assert controller["platform"] == "linux/amd64"
    assert controller["ports"] == ["127.0.0.1:${JENKINS_HTTP_PORT}:8080"]
    assert controller["restart"] == "no"
    assert controller["volumes"] == ["jenkins-home:/var/jenkins_home"]
    assert controller["secrets"] == [
        "jenkins-admin-password",
        "jenkins-handoff-password",
        "openai-api-key",
    ]
    assert compose["secrets"]["openai-api-key"] == {
        "file": "${JENKINS_OPENAI_API_KEY_FILE}"
    }
    assert set(controller["networks"]) == {
        "controller-egress",
        "controller-integration",
        "controller-ci",
        "controller-image-build",
        "controller-cv",
        "controller-cd",
        "runner-upstream-controller",
        "runner-fork-controller",
    }
    assert set(compose["volumes"]) == {
        "jenkins-home",
        "runner-upstream-state",
        "runner-fork-state",
        "buildkit-state",
        "pipeline-registry",
        "cv-engine-state",
        "k3s-kubeconfig",
    }
    assert "50000" not in (JENKINS_ROOT / "compose.yaml").read_text()


def test_jcasc_owns_zero_executor_security_and_retention_defaults() -> None:
    casc = _load_yaml(JENKINS_ROOT / "casc" / "jenkins.yaml")
    jenkins = casc["jenkins"]

    assert jenkins["numExecutors"] == 0
    assert jenkins["slaveAgentPort"] == -1
    assert jenkins["disableRememberMe"] is True
    assert jenkins["noUsageStatistics"] is True
    assert jenkins["scmCheckoutRetryCount"] == 0
    assert jenkins["globalNodeProperties"] == [
        {
            "envVars": {
                "env": [
                    {
                        "key": "SDI_REPOSITORY_URL",
                        "value": "${JENKINS_REPOSITORY_URL}",
                    },
                    {
                        "key": "SDI_FORK_REPOSITORY_URL",
                        "value": "${JENKINS_FORK_REPOSITORY_URL}",
                    },
                ]
            }
        }
    ]
    assert jenkins["securityRealm"]["local"]["allowsSignup"] is False
    assert jenkins["authorizationStrategy"]["projectMatrix"]["entries"] == [
        {
            "user": {
                "name": "${JENKINS_ADMIN_ID}",
                "permissions": ["Overall/Administer"],
            }
        },
        {
            "user": {
                "name": "${JENKINS_HANDOFF_ID}",
                "permissions": ["Overall/Read"],
            }
        },
    ]
    configured_discarders = casc["unclassified"]["buildDiscarders"][
        "configuredBuildDiscarders"
    ]
    assert configured_discarders[1]["simpleBuildDiscarder"]["discarder"][
        "logRotator"
    ] == {"artifactDaysToKeepStr": "90", "daysToKeepStr": "90"}
    assert casc["jobs"] == [
        {
            "providedEnv": {
                "HANDOFF_USER": "${JENKINS_HANDOFF_ID}",
                "REPOSITORY_URL": "${JENKINS_REPOSITORY_URL}",
                "FORK_REPOSITORY_URL": "${JENKINS_FORK_REPOSITORY_URL}",
            }
        },
        {"file": "/usr/local/share/jenkins/job-dsl/pipeline-integration.groovy"},
    ]
    assert casc["credentials"] == {
        "system": {
            "domainCredentials": [
                {
                    "credentials": [
                        {
                            "string": {
                                "scope": "GLOBAL",
                                "id": "sdi-openai-api-key",
                                "description": (
                                    "OpenAI API key bound around the composition Stage"
                                ),
                                "secret": "${openai-api-key}",
                            }
                        }
                    ]
                }
            ]
        }
    }
    assert casc["security"]["apiToken"] == {
        "creationOfLegacyTokenEnabled": False,
        "tokenGenerationOnCreationEnabled": False,
    }
    approved_signature = (
        "method org.jenkinsci.plugins.workflow.steps.FlowInterruptedException getCauses"
    )
    assert casc["security"]["scriptApproval"] == {
        "approvedSignatures": [approved_signature]
    }

    casc_text = (JENKINS_ROOT / "casc" / "jenkins.yaml").read_text()
    assert "jenkins-admin-password" in casc_text
    assert "jenkins-handoff-password" in casc_text
    assert "http://" not in casc_text
    assert "https://" not in casc_text


def test_job_dsl_defines_no_build_trigger_or_credential() -> None:
    # The smoke check verifies the rest of the job on the live configuration.
    job_dsl = (JENKINS_ROOT / "jobs" / "pipeline-integration.groovy").read_text()

    assert "trigger" not in job_dsl.lower()
    assert "credential" not in job_dsl.lower()


def test_root_pipeline_checks_out_the_submitted_commit() -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile").read_text()

    assert "$class: 'GitSCM'" not in pipeline
    assert (
        "reset_clone clone --filter=blob:none --no-checkout --no-tags --single-branch "
        '--branch main "$SDI_CHECKOUT_REPOSITORY_URL" .'
    ) in pipeline
    assert 'reset_checkout checkout --quiet --detach "$RESOLVED_COMMIT_SHA"' in pipeline
    assert (
        "until timeout 300 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 "
        '"$@"; do'
    ) in pipeline
    assert 'if [ "$attempt" -ge 3 ]; then' in pipeline
    assert pipeline.count("git_transfer_with_retries '") == 2
    assert "--depth" not in pipeline
    assert '"SDI_CHECKOUT_REPOSITORY_URL=${repositoryUrl}"' in pipeline
    assert "refs/remotes/origin/sdi-protected-main" in pipeline
    assert "git merge-base --is-ancestor" in pipeline


def test_root_pipeline_is_a_thin_scheduler_over_public_cli_operations() -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile").read_text()

    assert "env[name]" not in pipeline
    assert "checkout scm" not in pipeline
    assert "'pipeline-integration': env.SDI_REPOSITORY_URL," in pipeline
    assert "'pipeline-integration-fork': env.SDI_FORK_REPOSITORY_URL," in pipeline
    assert "][env.JOB_NAME]" in pipeline
    assert "timeout(time: runLimitSeconds, unit: 'SECONDS')" in pipeline
    assert "timeout(time: schedulingLimitSeconds, unit: 'SECONDS')" in pipeline
    assert (
        f"timeout(time: {RUN_LIMITS.finalization_reserve_seconds}, unit: 'SECONDS')"
        in pipeline
    )
    assert "timeout(time: definition.safetyTimeoutSeconds, unit: 'SECONDS')" in pipeline
    assert "stageExecutionStatus == 3" in pipeline
    assert "System.currentTimeMillis() < schedulingDeadlineEpochMillis" in pipeline
    assert "TimeoutStepExecution.ExceededTimeout" in pipeline
    assert "throw interruption" in pipeline
    assert "System.currentTimeMillis() >= runDeadlineEpochMillis" in pipeline
    assert len(re.findall(r"try \{\n\s+deleteDir\(\)", pipeline)) == 4
    assert pipeline.count("execute-jenkins-stage") == 1
    assert "--composition-config" not in pipeline
    assert "--generator" not in pipeline
    assert "preflight-jenkins-run" in pipeline
    assert "attempt-allows-continuation" in pipeline
    assert "finalize-jenkins-run" in pipeline
    assert "--run-deadline-expired" in pipeline
    assert "bundle-conclusion" in pipeline
    assert "stash(" in pipeline
    assert "unstash(" in pipeline
    assert "archiveArtifacts(" in pipeline
    assert "deleteDir()" in pipeline
    assert "retry(" not in pipeline
    assert "readJSON" not in pipeline
    assert '"execution_conclusion"' not in pipeline
    assert '"domain_outcome"' not in pipeline
    assert "lastBuild" not in pipeline


def test_cd_finalize_archives_the_deployment_manifest_after_the_bundle() -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile").read_text()
    finalize = pipeline[pipeline.index("stage('Finalize and archive')") :]
    steps = (
        "sdi-integration bundle-conclusion",
        "artifacts: '**/*'",
        (
            "sdi-integration deployment-manifest \\\n"
            "  --bundle-root bundle \\\n"
            f"  --output {DEPLOYMENT_MANIFEST_PATH}\n"
        ),
        (
            f"artifacts: '{DEPLOYMENT_MANIFEST_PATH}',\n"
            "                            allowEmptyArchive: true,"
        ),
        "if (conclusion == 1)",
    )

    positions = [finalize.index(step) for step in steps]

    assert positions == sorted(positions)
    assert pipeline.count(DEPLOYMENT_MANIFEST_PATH) == 2


def test_only_the_stack_refusal_names_a_limit_setting() -> None:
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "Jenkinsfile",
            "deployment",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    mentions = [
        (path, line.strip())
        for path in sorted(filter(None, listed.split("\0")))
        if (REPOSITORY_ROOT / path).is_file()
        for line in (REPOSITORY_ROOT / path).read_bytes().splitlines()
        if b"LIMIT_SECONDS" in line
    ]

    assert mentions == [
        (
            "deployment/jenkins/bin/stack",
            (
                b"JENKINS_*_LIMIT_SECONDS) fail 'delete JENKINS_*_LIMIT_SECONDS from"
                b" controller.env; limits come from the Stage grants in"
                b" _stage_registry.py' ;;"
            ),
        )
    ]


def test_root_pipeline_passes_each_stage_its_credentials() -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile").read_text()

    assert "work-limit" not in pipeline
    assert re.search(
        r"if \(definition\.credentials\) \{\s+"
        r"withCredentials\(definition\.credentials\.collect \{ credential ->\s+"
        r"string\(credentialsId: credential\.id, variable: credential\.variable\)\s+"
        r"\}\) \{\s+stageExecutionStatus = executeStage\(\)\s+\}\s+"
        r"\} else \{\s+stageExecutionStatus = executeStage\(\)\s+\}",
        pipeline,
    )


def test_root_pipeline_reports_stale_agent_images_as_their_typed_reason() -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile").read_text()

    assert f"currentBuild.description = '{STALE_IMAGE_REASON}'" in pipeline
    assert "preflightStatus == 4" in pipeline
    assert "stageExecutionStatus == 4" in pipeline
    assert pipeline.count("--baked-code-digest") == 1
    assert pipeline.count("--integration-baked-code-digest") == 1
    assert pipeline.count("--stage-baked-code-digest") == 1


def _fresh_image_step(pipeline: str, digest_file: Path) -> str:
    command = re.search(
        r"^def bakedCodeDigestCommand = '(.*)'$", pipeline, re.MULTILINE
    )
    step = re.search(
        r'^def requireFreshAgentImage = """(.*?)"""$',
        pipeline,
        re.MULTILINE | re.DOTALL,
    )
    assert command
    assert step
    return (
        step.group(1)
        .replace("${bakedCodeDigestCommand}", command.group(1))
        .replace("\\$", "$")
        .replace("\\\\", "\\")
        .replace("/etc/sdi/integration-code-digest", str(digest_file))
    )


OTHER_COMMIT_PREFLIGHT = (
    "sdi-integration preflight-jenkins-run --composition-node ci"
    " --composition-baked-code-digest missing\n"
)


@pytest.mark.parametrize(
    ("baked", "status"),
    [("0" * 64, 4), ("", 4), (None, 4), ("checkout", 2)],
)
def test_stale_image_fails_typed_before_any_flag_is_parsed(
    tmp_path: Path, baked: str | None, status: int
) -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile").read_text()
    digest_file = tmp_path / "integration-code-digest"
    if baked == "checkout":
        baked = integration_code_digest(REPOSITORY_ROOT / "integration")
    if baked is not None:
        digest_file.write_text(baked)
    script = f"set -eu\n{_fresh_image_step(pipeline, digest_file)}\n"

    executed = subprocess.run(
        ["sh", "-c", script + OTHER_COMMIT_PREFLIGHT],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert executed.returncode == status, executed.stderr
    if status == 4:
        assert executed.stderr.startswith(f"{STALE_IMAGE_REASON}: "), executed.stderr
    else:
        assert "the following arguments are required" in executed.stderr


def test_live_check_compiles_the_tracked_jenkinsfile_with_workflow_cps(  # noqa: C901
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, str]] = []
    compile_response: dict[str, object] = {
        "status": "success",
        "message": "",
        "line": 0,
        "column": 0,
    }
    agent_labels = {
        "integration": "integration",
        "ci": "composition",
        "image-build": "image-build",
        "cv": "cv",
        "cd": "cd",
    }

    class JenkinsHandler(BaseHTTPRequestHandler):
        def _respond(
            self,
            status: int,
            body: bytes,
            content_type: str = "application/json",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Jenkins", "2.568.3")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            requests.append(("GET", self.path))
            if self.path == "/crumbIssuer/api/json":
                body = b'{"crumbRequestField":"Jenkins-Crumb","crumb":"test"}'
            elif self.path.startswith("/api/json?tree=numExecutors"):
                body = b'{"numExecutors":0,"slaveAgentPort":-1}'
            elif self.path.startswith("/computer/api/json"):
                computers = [
                    {
                        "displayName": "Built-In Node",
                        "numExecutors": 0,
                        "offline": False,
                        "temporarilyOffline": False,
                        "idle": True,
                    },
                    *(
                        {
                            "displayName": name,
                            "numExecutors": 1,
                            "offline": False,
                            "temporarilyOffline": False,
                            "idle": True,
                        }
                        for name in agent_labels
                    ),
                ]
                body = json.dumps({"computer": computers}).encode()
            elif self.path.startswith("/computer/"):
                name = urllib.parse.unquote(self.path.split("/")[2])
                body = f"<slave><label>{agent_labels[name]}</label></slave>".encode()
                self._respond(200, body, "application/xml")
                return
            elif self.path.startswith("/pluginManager/api/json"):
                body = json.dumps(
                    {
                        "plugins": [
                            {
                                "shortName": "workflow-cps",
                                "version": "4378.v7a_08f1b_b_f8f4",
                                "active": True,
                                "enabled": True,
                            }
                        ]
                    }
                ).encode()
            elif self.path in JOB_REPOSITORY_URLS:
                repository_url = JOB_REPOSITORY_URLS[self.path]
                parameters = "".join(
                    "<hudson.model.StringParameterDefinition>"
                    f"<name>{name}</name>"
                    "</hudson.model.StringParameterDefinition>"
                    for name in HANDOFF_PARAMETERS
                )
                permissions = "".join(
                    f"<permission>{permission}</permission>"
                    for permission in (
                        "USER:hudson.model.Item.Read:github-handoff",
                        "USER:hudson.model.Item.Build:github-handoff",
                        "USER:hudson.model.Item.Cancel:github-handoff",
                    )
                )
                body = (
                    "<flow-definition><properties>"
                    "<org.jenkinsci.plugins.workflow.job.properties."
                    "DisableConcurrentBuildsJobProperty/>"
                    "<hudson.model.ParametersDefinitionProperty>"
                    f"<parameterDefinitions>{parameters}</parameterDefinitions>"
                    "</hudson.model.ParametersDefinitionProperty>"
                    "</properties><logRotator><daysToKeep>90</daysToKeep>"
                    "<artifactDaysToKeep>90</artifactDaysToKeep></logRotator>"
                    "<definition><scm><userRemoteConfigs><hudson.plugins.git."
                    f"UserRemoteConfig><url>{repository_url}</url>"
                    "<refspec>+refs/heads/main:refs/remotes/origin/main</refspec>"
                    "</hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>"
                    "<branches><hudson.plugins.git.BranchSpec>"
                    "<name>${RESOLVED_COMMIT_SHA}</name>"
                    "</hudson.plugins.git.BranchSpec></branches><extensions>"
                    "<hudson.plugins.git.extensions.impl.CloneOption>"
                    "<honorRefspec>true</honorRefspec>"
                    "<noTags>true</noTags>"
                    "</hudson.plugins.git.extensions.impl.CloneOption>"
                    "<hudson.plugins.git.extensions.impl.CleanBeforeCheckout/>"
                    "</extensions></scm>"
                    "<scriptPath>Jenkinsfile</scriptPath>"
                    "<lightweight>false</lightweight></definition>"
                    f"<authorizationMatrixProperty>{permissions}"
                    "</authorizationMatrixProperty></flow-definition>"
                ).encode()
                self._respond(200, body, "application/xml")
                return
            elif self.path in {
                "/job/pipeline-integration/api/json",
                "/job/pipeline-integration-fork/api/json",
            }:
                body = b"{}"
            elif self.path == "/script":
                self._respond(403, b"")
                return
            else:
                self._respond(404, b"")
                return
            self._respond(200, body)

        def do_POST(self) -> None:
            requests.append(("POST", self.path))
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if self.path == "/configuration-as-code/check":
                self._respond(200, b"[]")
            elif self.path.endswith("/checkScriptCompile"):
                assert urllib.parse.parse_qs(body.decode())["value"] == [
                    (REPOSITORY_ROOT / "Jenkinsfile").read_text(encoding="utf-8")
                ]
                self._respond(200, json.dumps(compile_response).encode())
            elif self.path == "/scriptText":
                self._respond(
                    200,
                    (
                        b"method org.jenkinsci.plugins.workflow.steps."
                        b"FlowInterruptedException getCauses\n"
                    ),
                    "text/plain",
                )
            else:
                self._respond(404, b"")

        def log_message(self, format: str, *arguments: object) -> None:  # noqa: A002
            del format, arguments

    admin_password = tmp_path / "admin-password"
    handoff_password = tmp_path / "handoff-password"
    plugin_lock = tmp_path / "plugins.txt"
    casc = tmp_path / "jenkins.yaml"
    admin_password.write_text("admin-password\n", encoding="utf-8")
    handoff_password.write_text("handoff-password\n", encoding="utf-8")
    plugin_lock.write_text("workflow-cps:4378.v7a_08f1b_b_f8f4\n", encoding="utf-8")
    casc.write_text("jenkins: {}\n", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), JenkinsHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    command = [
        SMOKE_CHECK,
        "check",
        "--url",
        f"http://127.0.0.1:{server.server_port}",
        "--admin-id",
        "administrator",
        "--admin-password-file",
        admin_password,
        "--handoff-id",
        "github-handoff",
        "--handoff-password-file",
        handoff_password,
        "--repository-url",
        "https://github.com/example/repository.git",
        "--fork-repository-url",
        "https://github.com/example-fork/repository.git",
        "--plugin-lock",
        plugin_lock,
        "--casc",
        casc,
        "--jenkinsfile",
        REPOSITORY_ROOT / "Jenkinsfile",
    ]
    try:
        accepted = subprocess.run(command, check=False, capture_output=True, text=True)
        compile_response.update(
            {
                "status": "fail",
                "message": "unexpected token",
                "line": 7,
                "column": 3,
            }
        )
        rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert accepted.returncode == 0, accepted.stderr
    assert any(path.endswith("/checkScriptCompile") for _method, path in requests)
    assert rejected.returncode == 1
    assert "Jenkinsfile compilation failed at line 7, column 3" in rejected.stderr


def test_stack_validate_uses_compose_and_rejects_insecure_secret_files(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_fake_deployment_tools(fake_bin)

    admin_secret = tmp_path / "admin-password"
    handoff_secret = tmp_path / "handoff-password"
    admin_secret.write_text("admin-secret\n")
    handoff_secret.write_text("handoff-secret\n")
    admin_secret.chmod(0o600)
    handoff_secret.chmod(0o644)
    openai_secret = tmp_path / "openai-api-key"
    openai_secret.write_text("openai-api-key\n")
    openai_secret.chmod(0o600)
    agent_secrets = {
        name: tmp_path / f"{name}-agent-secret"
        for name in ("integration", "ci", "image-build", "cv", "cd")
    }
    for index, secret in enumerate(agent_secrets.values(), start=10):
        secret.write_text(f"{index:x}" * 64)
        secret.chmod(0o600)
    config = tmp_path / "controller.env"
    setup = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "setup"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert setup.returncode == 0, setup.stderr
    assert config.stat().st_mode & 0o777 == 0o600
    config.write_text(
        "JENKINS_HTTP_PORT=8080\n"
        "JENKINS_ADMIN_ID=administrator\n"
        "JENKINS_HANDOFF_ID=github-handoff\n"
        "JENKINS_REPOSITORY_URL=https://github.com/example/repository.git\n"
        "JENKINS_FORK_REPOSITORY_URL=https://github.com/example-fork/repository.git\n"
        "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080\n"
        f"JENKINS_ADMIN_PASSWORD_FILE={admin_secret}\n"
        f"JENKINS_HANDOFF_PASSWORD_FILE={handoff_secret}\n"
        f"JENKINS_OPENAI_API_KEY_FILE={openai_secret}\n"
        f"JENKINS_INTEGRATION_AGENT_SECRET_FILE={agent_secrets['integration']}\n"
        f"JENKINS_CI_AGENT_SECRET_FILE={agent_secrets['ci']}\n"
        f"JENKINS_IMAGE_BUILD_AGENT_SECRET_FILE={agent_secrets['image-build']}\n"
        f"JENKINS_CV_AGENT_SECRET_FILE={agent_secrets['cv']}\n"
        f"JENKINS_CD_AGENT_SECRET_FILE={agent_secrets['cd']}\n"
    )

    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert "must not be accessible by group or other users" in completed.stderr

    handoff_secret.chmod(0o200)
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert "must be owner-readable" in completed.stderr

    handoff_secret.chmod(0o600)
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    validation_log = docker_log.read_text()
    assert "compose" in validation_log
    assert "config --quiet" in validation_log
    assert f"images={'|'.join(FAKE_AGENT_IMAGES)}" in validation_log

    openai_secret.chmod(0o640)
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert "must not be accessible by group or other users" in completed.stderr
    openai_secret.chmod(0o600)

    valid_config = config.read_text()
    fork_url = "https://github.com/example-fork/repository.git"
    for valid, invalid, message in (
        (
            fork_url,
            "https://github.com/example/repository.git",
            "JENKINS_REPOSITORY_URL and JENKINS_FORK_REPOSITORY_URL must differ",
        ),
        (
            fork_url,
            "https://github.com/example-fork@token/repository.git",
            "JENKINS_FORK_REPOSITORY_URL must not contain credentials",
        ),
    ):
        config.write_text(valid_config.replace(valid, invalid))
        completed = subprocess.run(
            [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
            check=False,
            capture_output=True,
            env={
                "DOCKER_LOG": str(docker_log),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            },
            text=True,
        )
        assert completed.returncode == 2
        assert message in completed.stderr
    config.write_text(valid_config)

    agent_secrets["cd"].unlink()
    preflight = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "preflight"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert preflight.returncode == 0, preflight.stderr
    preflight_log = docker_log.read_text()
    assert ":/var/jenkins_home --publish 127.0.0.1::8080" in preflight_log
    assert "-c touch /var/jenkins_home/write-test" in preflight_log
    agent_secrets["cd"].write_text("e" * 64)
    agent_secrets["cd"].chmod(0o600)

    valid_config = config.read_text()
    config.write_text(f"{valid_config}JENKINS_CV_WORK_LIMIT_SECONDS=2700\n")
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stderr == (
        "stack: delete JENKINS_*_LIMIT_SECONDS from controller.env;"
        " limits come from the Stage grants in _stage_registry.py\n"
    )
    config.write_text(valid_config)

    _assert_plaintext_relocated_agent_url_is_rejected(config, fake_bin, docker_log)

    agent_secrets["ci"].write_text(agent_secrets["integration"].read_text())
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert "registration secrets must not be shared" in completed.stderr
    agent_secrets["ci"].write_text("b" * 64)

    subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "start"],
        check=True,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "stop"],
        check=True,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    lifecycle_log = docker_log.read_text()
    _assert_stack_lifecycle_log(lifecycle_log)

    forced_stop = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "stop"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "FORCE_KILL": "1",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert forced_stop.returncode == 2
    assert "required forced termination" in forced_stop.stderr

    invalid_project = subprocess.run(
        [
            JENKINS_ROOT / "bin" / "stack",
            "--project-name",
            "INVALID",
            "--config",
            config,
            "validate",
        ],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert invalid_project.returncode == 2
    assert "project name must start" in invalid_project.stderr

    _assert_single_agent_reconciliation(
        config,
        fake_bin,
        docker_log,
        agent_secrets["integration"],
    )

    config.write_text("NOT_A_CONTROLLER_SETTING=value\n")
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "stop"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    missing_config = tmp_path / "missing.env"
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", missing_config, "stop"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_smoke_prunes_only_dangling_sdi_images() -> None:
    smoke = (JENKINS_ROOT / "bin" / "smoke").read_text()
    assert (
        "docker image prune --force \\\n"
        "        --filter label=org.sdi.project=ci-cv-cd_framework"
    ) in smoke
    assert "prune --all" not in smoke
    assert "builder prune" not in smoke


def test_smoke_validates_and_builds_each_image_once(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    jenkins = checkout / "deployment" / "jenkins"
    (jenkins / "bin").mkdir(parents=True)
    for entry in JENKINS_ROOT.iterdir():
        if entry.name not in {"bin", "config"}:
            (jenkins / entry.name).symlink_to(entry)
    (checkout / "deployment" / "installation.env").symlink_to(
        REPOSITORY_ROOT / "deployment" / "installation.env"
    )
    for name in ("smoke", "stack"):
        (jenkins / "bin" / name).symlink_to(JENKINS_ROOT / "bin" / name)
    checker = jenkins / "bin" / "smoke-check"
    checker.write_text(
        "#!/bin/sh\n"
        '[ "$1" = provision-secrets ] || exit 0\n'
        'while [ "$#" -gt 0 ]; do\n'
        '    [ "$1" != --agent-secret-directory ] || directory=$2\n'
        "    shift\n"
        "done\n"
        "for name in integration ci image-build cv cd; do\n"
        "    od -An -tx1 -N32 /dev/urandom | tr -d ' \\n' "
        '> "$directory/$name-agent-secret"\n'
        "done\n"
    )
    checker.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_deployment_tools(fake_bin)
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    docker_log = tmp_path / "docker.log"

    completed = subprocess.run(
        [jenkins / "bin" / "smoke"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TMPDIR": str(temporary),
        },
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("tracked sources match") == 1
    log = docker_log.read_text()
    for dockerfile in (
        "controller/Dockerfile",
        "agents/Dockerfile",
        "runner/Dockerfile",
        "runner-proxy/Dockerfile",
    ):
        assert (
            len(re.findall(rf"build --check .*--file \S*/{dockerfile} ", log)) == 1
        ), dockerfile
    assert log.count("--tag sdi-jenkins-controller:2.568.3") == 1
    assert log.count(" build integration ci image-build cv cd\n") == 1
    assert log.count(" build runner-fork runner-fork-proxy\n") == 2
    assert log.count("up --no-build --detach --wait --wait-timeout 240\n") == 3
    assert "sdi-cv-warm" not in log
    adapter_image_calls = [line for line in log.splitlines() if "adapter-image" in line]
    assert adapter_image_calls
    assert all(STAGE_ARGUMENTS in line for line in adapter_image_calls)


class _BuiltStack:
    def __init__(self, root: Path) -> None:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        _write_fake_deployment_tools(fake_bin)
        self.docker_log = root / "docker.log"
        secrets: dict[str, Path] = {}
        for index, name in enumerate(
            (
                "admin",
                "handoff",
                "openai",
                "integration",
                "ci",
                "image-build",
                "cv",
                "cd",
            ),
            start=1,
        ):
            secrets[name] = root / f"{name}-secret"
            secrets[name].write_text(f"{index:x}" * 64)
            secrets[name].chmod(0o600)
        self.config = root / "controller.env"
        self.config.write_text(
            "JENKINS_HTTP_PORT=8080\n"
            "JENKINS_ADMIN_ID=administrator\n"
            "JENKINS_HANDOFF_ID=github-handoff\n"
            "JENKINS_REPOSITORY_URL=https://github.com/example/repository.git\n"
            "JENKINS_FORK_REPOSITORY_URL=https://github.com/example-fork/repository.git\n"
            "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080\n"
            f"JENKINS_ADMIN_PASSWORD_FILE={secrets['admin']}\n"
            f"JENKINS_HANDOFF_PASSWORD_FILE={secrets['handoff']}\n"
            f"JENKINS_OPENAI_API_KEY_FILE={secrets['openai']}\n"
            f"JENKINS_INTEGRATION_AGENT_SECRET_FILE={secrets['integration']}\n"
            f"JENKINS_CI_AGENT_SECRET_FILE={secrets['ci']}\n"
            f"JENKINS_IMAGE_BUILD_AGENT_SECRET_FILE={secrets['image-build']}\n"
            f"JENKINS_CV_AGENT_SECRET_FILE={secrets['cv']}\n"
            f"JENKINS_CD_AGENT_SECRET_FILE={secrets['cd']}\n"
        )
        self.path = f"{fake_bin}:{os.environ['PATH']}"

    def run(self, *arguments: str, **state: str) -> subprocess.CompletedProcess[str]:
        self.docker_log.write_text("")
        return subprocess.run(
            [JENKINS_ROOT / "bin" / "stack", "--config", self.config, *arguments],
            check=False,
            capture_output=True,
            env={"DOCKER_LOG": str(self.docker_log), "PATH": self.path} | state,
            text=True,
        )

    def calls(self) -> list[str]:
        return [
            line.split("/compose.yaml ", 1)[-1]
            for line in self.docker_log.read_text().splitlines()
            if line.startswith(("compose ", "rm ", "ps "))
        ]

    def refuses_prebuilt(self, action: str, **state: str) -> bool:
        completed = self.run("--prebuilt", action, **state)
        return (
            completed.returncode == 2
            and f"run: deployment/jenkins/bin/stack --config {self.config} build"
            in completed.stderr
            and " up " not in self.docker_log.read_text()
        )


@pytest.mark.parametrize(
    "action", ["start-controller", "start", "verify-live", "exercise-live"]
)
def test_prebuilt_actions_require_a_build_of_this_checkout(
    tmp_path: Path, action: str
) -> None:
    stack = _BuiltStack(tmp_path)

    assert stack.refuses_prebuilt(action)

    built = stack.run("build")
    assert built.returncode == 0, built.stderr
    assert stack.refuses_prebuilt(action, FAKE_TREE="2")
    assert stack.refuses_prebuilt(action, FAKE_IMAGE_ID="2")
    assert stack.refuses_prebuilt(action, FAKE_NO_IMAGES="1")


@pytest.mark.parametrize("action", ["start-controller", "start"])
def test_prebuilt_start_reuses_a_build_of_this_checkout(
    tmp_path: Path, action: str
) -> None:
    stack = _BuiltStack(tmp_path)
    built = stack.run("build")
    assert built.returncode == 0, built.stderr

    completed = stack.run("--prebuilt", action)

    assert completed.returncode == 0, completed.stderr
    log = stack.docker_log.read_text()
    assert "validate-installation" not in log
    assert "build --check" not in log
    assert "--tag sdi-jenkins-controller" not in log
    assert " up --no-build --detach --wait" in log


def test_build_refuses_an_agent_image_that_declares_a_volume(tmp_path: Path) -> None:
    stack = _BuiltStack(tmp_path)

    completed = stack.run("build", FAKE_AGENT_VOLUMES='{"/var/lib/data":{}}')

    assert completed.returncode == 2
    assert (
        "integration agent image declares an unexpected persistent volume"
        in completed.stderr
    )
    assert stack.refuses_prebuilt("start")


@pytest.mark.parametrize("action", ["build", "validate", "destroy"])
def test_prebuilt_applies_only_to_start_and_live_checks(
    tmp_path: Path, action: str
) -> None:
    completed = _BuiltStack(tmp_path).run("--prebuilt", action)

    assert completed.returncode == 2
    assert f"--prebuilt does not apply to {action}" in completed.stderr


WARM_UP = "up --no-build --detach --wait --wait-timeout 240 cv-engine"
WARM = "run --rm --no-deps -T --name sdi-jenkins-cv-warm --entrypoint sdi-cv-warm cv"
FULL_UP = "up --no-build --detach --wait --wait-timeout 240"


def test_start_warms_the_cv_engine_before_any_run_can_reach_it(
    tmp_path: Path,
) -> None:
    stack = _BuiltStack(tmp_path)

    completed = stack.run("start")

    assert completed.returncode == 0, completed.stderr
    calls = stack.calls()
    runner_check = next(
        index for index, call in enumerate(calls) if call.startswith("ps ")
    )
    assert calls.index(WARM_UP) < calls.index("rm --force sdi-jenkins-cv-warm")
    assert calls.index("rm --force sdi-jenkins-cv-warm") < calls.index(WARM)
    assert calls.index(WARM) < runner_check < calls.index(FULL_UP)


K3S_RM = "rm --force --stop --volumes k3s-xycar k3s-burger k3s-waffle k3s-orin"


def test_start_replaces_the_k3s_cluster_after_the_runner_check(
    tmp_path: Path,
) -> None:
    stack = _BuiltStack(tmp_path)

    completed = stack.run("start")

    assert completed.returncode == 0, completed.stderr
    calls = stack.calls()
    runner_check = next(
        index for index, call in enumerate(calls) if call.startswith("ps ")
    )
    assert runner_check < calls.index(K3S_RM) < calls.index(FULL_UP)
    assert (
        "pull --quiet --policy missing buildkitd registry cv-engine"
        " k3s-xycar k3s-burger k3s-waffle k3s-orin"
    ) in calls


def test_stop_removes_the_k3s_cluster_before_it_checks_for_forced_kills(
    tmp_path: Path,
) -> None:
    stack = _BuiltStack(tmp_path)

    completed = stack.run("stop", FAKE_K3S_UP="1")

    assert completed.returncode == 0, completed.stderr
    assert K3S_RM in stack.calls()


def test_start_without_the_cv_warm_leaves_the_engine_cold(tmp_path: Path) -> None:
    stack = _BuiltStack(tmp_path)

    completed = stack.run("--no-cv-warm", "start")

    assert completed.returncode == 0, completed.stderr
    calls = stack.calls()
    assert WARM_UP not in calls
    assert WARM not in calls
    assert FULL_UP in calls


def test_start_stops_before_the_agents_when_the_cv_warm_fails(tmp_path: Path) -> None:
    stack = _BuiltStack(tmp_path)

    completed = stack.run("start", FAKE_WARM_FAIL="1")

    assert completed.returncode == 2
    assert completed.stderr == (
        "stack: the CV engine has no simulation image, so start did not start"
        " the agents and runners; rerun: deployment/jenkins/bin/stack start\n"
    )
    assert FULL_UP not in stack.calls()


@pytest.mark.parametrize("action", ["start-controller", "verify-live", "destroy"])
def test_no_cv_warm_applies_only_to_start(tmp_path: Path, action: str) -> None:
    stack = _BuiltStack(tmp_path)

    completed = stack.run("--no-cv-warm", action)

    assert completed.returncode == 2
    assert completed.stderr == "stack: --no-cv-warm applies only to start\n"
    assert not stack.calls()


# A launcher that backgrounds pytest leaves SIGINT ignored, and sh can trap no
# signal it inherits as ignored, so start the stack with SIGINT at its default,
# as a terminal's foreground job has it.
WITH_DEFAULT_SIGINT = (
    sys.executable,
    "-c",
    (
        "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); "
        "os.execv(sys.argv[1], sys.argv[1:])"
    ),
)


def test_interrupting_start_mid_warm_removes_the_warm_container(
    tmp_path: Path,
) -> None:
    stack = _BuiltStack(tmp_path)
    stack.docker_log.write_text("")
    started = subprocess.Popen(
        [
            *WITH_DEFAULT_SIGINT,
            JENKINS_ROOT / "bin" / "stack",
            "--config",
            stack.config,
            "start",
        ],
        env={
            "DOCKER_LOG": str(stack.docker_log),
            "PATH": stack.path,
            "FAKE_WARM_HANG": "1",
        },
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20
    while WARM not in stack.calls():
        assert started.poll() is None, "start ended before the warm ran"
        assert time.monotonic() < deadline, "the warm never ran"
        time.sleep(0.1)

    os.killpg(started.pid, signal.SIGINT)

    assert started.wait(timeout=20) == 130
    calls = stack.calls()
    assert calls[calls.index(WARM) + 1 :] == ["rm --force sdi-jenkins-cv-warm"]

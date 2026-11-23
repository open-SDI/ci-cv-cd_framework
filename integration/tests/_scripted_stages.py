from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sdi_pipeline_integration._integration_code import integration_code_digest
from sdi_pipeline_integration._stage_registry import AGENTS, INTEGRATION_AGENT, STAGES
from tests import _cd_evidence as cd
from tests._cv_evidence import AMD64_DIGEST, SERVICE_IDS
from tests._event_console import Console
from tests._fixture_inputs import (
    FIXTURE_COMPOSITION_CONFIG,
    FIXTURE_SERVICE_REPOSITORY,
    STAGE_DESCRIPTORS,
    copy_baked_integration_code,
    write_fixture_composition_descriptor,
    write_scripted_adapter,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sdi_pipeline_integration._stage_registry import StageName

SOURCE_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_ID = "12345678-1234-4234-9234-123456789abc"
SUCCESS_REQUEST = "runs/s-04/s-04-tc-03-c-05-fixture.yaml"
COMPOSITION_CONFIG_ARGUMENTS = ("--composition-config", FIXTURE_COMPOSITION_CONFIG)
COMMITTED_FILES = (
    SUCCESS_REQUEST,
    "requirements/s-04/deliver-book-to-joe.md",
    "profiles/s-04/waffle-native-arm64.yaml",
    "profiles/s-04/waffle-jetson-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    *STAGE_DESCRIPTORS,
    "runs/conformance/composition-negative-domain.yaml",
    "runs/conformance/composition-handled-failure.yaml",
    "runs/conformance/composition-malformed-response.yaml",
    "runs/conformance/composition-timeout.yaml",
    FIXTURE_COMPOSITION_CONFIG,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def jenkins_repository(
    tmp_path: Path,
    *,
    fixture_image_build: bool = False,
    service_repository: Path = FIXTURE_SERVICE_REPOSITORY,
) -> tuple[Path, str]:
    """Commit the Stage inputs; a Fixture image-build completes the chain."""
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Test Operator")
    git(repository, "config", "user.email", "operator@example.test")
    git(
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
    shutil.copytree(service_repository, repository / "service-repository")
    copy_baked_integration_code(repository)
    write_fixture_composition_descriptor(repository)
    adapters = repository / "deployment/jenkins/adapters"
    _run_the_cd_fixture_without_a_cluster(adapters)
    if fixture_image_build:
        (adapters / "image-build-v1.yaml").write_bytes(
            (adapters / "image-build-fixture-v1.yaml").read_bytes()
        )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Add distributed Fixture inputs")
    return repository, git(repository, "rev-parse", "HEAD")


def _run_the_cd_fixture_without_a_cluster(adapters: Path) -> None:
    (adapters / "cd-v1.yaml").write_bytes(
        (adapters / "cd-fixture-v1.yaml").read_bytes()
    )


def common_arguments(repository: Path, commit_sha: str, request_path: str) -> list[str]:
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


def baked_code_digest(repository: Path) -> str:
    """Record the checkout's integration code as a freshly built image would."""
    return integration_code_digest(repository / "integration")


def fresh_image(repository: Path) -> tuple[str, str]:
    return ("--baked-code-digest", baked_code_digest(repository))


def run_preflight(  # noqa: PLR0913
    repository: Path,
    commit_sha: str,
    request_path: str,
    started_millis: str,
    *,
    baked: dict[str, str] | None = None,
    console: Console | None = None,
) -> subprocess.CompletedProcess[str]:
    fresh = baked_code_digest(repository)
    recorded = dict.fromkeys((agent.node for agent in AGENTS), fresh) | (baked or {})
    return (console or Console()).run(
        "preflight-jenkins-run",
        *common_arguments(repository, commit_sha, request_path),
        "--composition-config",
        FIXTURE_COMPOSITION_CONFIG,
        "--handoff-contract-version",
        "sdi.github-jenkins-handoff/v1",
        "--github-run-id",
        "1000",
        "--github-run-attempt",
        "1",
        "--build-started-at-millis",
        started_millis,
        "--integration-node",
        INTEGRATION_AGENT.node,
        "--integration-baked-code-digest",
        recorded[INTEGRATION_AGENT.node],
        *(
            argument
            for name, stage in STAGES.items()
            for argument in (
                "--stage-node",
                f"{name}={stage.agent.node}",
                "--stage-baked-code-digest",
                f"{name}={recorded[stage.agent.node]}",
            )
        ),
    )


def millis_from_now(seconds: float) -> str:
    return str(int((time.time() + seconds) * 1000))


@dataclass
class JenkinsRun:
    repository: Path
    commit_sha: str
    root: Path
    request_path: str = SUCCESS_REQUEST
    started_millis: str = field(default_factory=lambda: millis_from_now(-1))
    console: Console = field(default_factory=Console)
    attempts: list[Path] = field(default_factory=list)

    @property
    def bundle_root(self) -> Path:
        return self.root / "bundle"

    def preflight(self) -> subprocess.CompletedProcess[str]:
        return run_preflight(
            self.repository,
            self.commit_sha,
            self.request_path,
            self.started_millis,
            console=self.console,
        )

    def stage_arguments(
        self,
        stage: StageName,
        *,
        deadline_millis: str,
        work_limit: str | None = None,
        baked: str | None = None,
    ) -> list[str]:
        return [
            "execute-jenkins-stage",
            *common_arguments(self.repository, self.commit_sha, self.request_path),
            "--baked-code-digest",
            baked or baked_code_digest(self.repository),
            *(COMPOSITION_CONFIG_ARGUMENTS if stage == "composition" else ()),
            "--stage",
            stage,
            "--attempt-root",
            str(self.root / "transfers" / stage),
            "--run-deadline-epoch-millis",
            deadline_millis,
            *(() if work_limit is None else ("--work-limit-seconds", work_limit)),
            *(
                argument
                for prior in self.attempts
                for argument in ("--prior-attempt-root", str(prior))
            ),
        ]

    def execute(
        self,
        stage: StageName,
        *,
        deadline_millis: str | None = None,
        work_limit: str | None = None,
        baked: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        executed = self.console.run(
            *self.stage_arguments(
                stage,
                deadline_millis=deadline_millis or millis_from_now(60),
                work_limit=work_limit,
                baked=baked,
            )
        )
        if executed.returncode == 0:
            self.attempts.append(self.root / "transfers" / stage)
        return executed

    def continues(self, stage: StageName) -> bool:
        continuation = self.console.run(
            "attempt-allows-continuation",
            "--attempt-root",
            str(self.root / "transfers" / stage),
            "--execution-id",
            EXECUTION_ID,
            "--stage",
            stage,
        )
        assert continuation.returncode in {0, 1}, continuation.stderr
        return continuation.returncode == 0

    def finalize(
        self,
        *,
        attempts: Sequence[Path] | None = None,
        run_deadline_expired: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self.console.run(
            "finalize-jenkins-run",
            *common_arguments(self.repository, self.commit_sha, self.request_path),
            "--build-started-at-millis",
            self.started_millis,
            *(("--run-deadline-expired",) if run_deadline_expired else ()),
            "--bundle-root",
            str(self.bundle_root),
            *(
                argument
                for attempt in (self.attempts if attempts is None else attempts)
                for argument in ("--attempt-root", str(attempt))
            ),
        )

    def conclusion(self) -> subprocess.CompletedProcess[str]:
        return self.console.run(
            "bundle-conclusion",
            "--bundle-root",
            str(self.bundle_root),
            "--execution-id",
            EXECUTION_ID,
        )

    def chain(
        self,
        run_limit_seconds: int = 60,
        work_limits: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        preflighted = self.preflight()
        assert preflighted.returncode == 0, preflighted.stderr
        assert json.loads(preflighted.stdout)["execution_id"] == EXECUTION_ID
        deadline = millis_from_now(run_limit_seconds)
        for stage in STAGES:
            executed = self.execute(
                stage,
                deadline_millis=deadline,
                work_limit=(work_limits or {}).get(stage),
            )
            assert executed.returncode == 0, executed.stderr
            if not self.continues(stage):
                break
        finalized = self.finalize()
        assert finalized.returncode == 0, finalized.stderr
        return self.conclusion(), json.loads(finalized.stdout)


def execute_chain(  # noqa: PLR0913, PLR0917
    repository: Path,
    commit_sha: str,
    request_path: str,
    root: Path,
    started_millis: str,
    run_limit_seconds: int = 60,
    work_limits: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run every Stage, each given the supplied inputs it is granted."""
    run = JenkinsRun(repository, commit_sha, root, request_path, started_millis)
    return run.chain(run_limit_seconds, work_limits)


# Both profile hosts are arm64; mission control stays unresolved yet keeps a host.
PLACEMENTS = [
    {
        "service_id": "fixture-localization",
        "status": "resolved",
        "artifact_id": "debs",
        "host": "orin",
    },
    {
        "service_id": "fixture-navigation",
        "status": "resolved",
        "artifact_id": "debs",
        "host": "waffle",
    },
    {"service_id": "fixture-mission-control", "status": "unresolved", "host": "orin"},
]
DIGEST = f"sha256:{'a' * 64}"


def write_response_adapter(
    path: Path, documents: dict[str, str], domain_outcome: str
) -> None:
    """Write an adapter that publishes fixed documents, naming its inputs."""
    write_scripted_adapter(
        path,
        f"""(output / "outputs").mkdir()
sources = [
    {{"slot": item["slot"], "sha256": item["sha256"]}} for item in request["inputs"]
]
for name, document in {documents!r}.items():
    payload = json.loads(document)
    payload["source_inputs"] = sources
    (output / "outputs" / name).write_text(json.dumps(payload))
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": {domain_outcome!r},
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [item["slot"] for item in request["outputs"]
        if item["role"] == "domain_output"],
    "diagnostic": {{"present": False, "truncated": False}},
}}
(output / "response.json").write_text(json.dumps(response))
""",
    )


def implemented_upstream(
    tmp_path: Path,
    images: dict[str, dict[str, Any]] | None,
    service_repository: Path = FIXTURE_SERVICE_REPOSITORY,
    placements: list[dict[str, str]] = PLACEMENTS,
) -> Path:
    repository, _commit_sha = jenkins_repository(
        tmp_path, service_repository=service_repository
    )
    case = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
    blueprint = json.loads((case / "composition-blueprint.json").read_text())
    deployment = json.loads((case / "deployment-schema.json").read_text())
    composition = tmp_path / "implemented-composition-adapter"
    write_response_adapter(
        composition,
        {
            "composition-blueprint.json": json.dumps(
                blueprint | {"evidence_basis": "implemented"}
            ),
            "deployment-schema.json": json.dumps(
                deployment | {"evidence_basis": "implemented", "placements": placements}
            ),
        },
        "succeeded",
    )
    write_fixture_composition_descriptor(
        repository, entrypoint=composition, mode="implemented"
    )
    image_build = tmp_path / "implemented-image-build-adapter"
    result = {
        "schema_version": "sdi.image-build-result/v1",
        "evidence_basis": "implemented",
        "image_build_result_id": "s-04-tc-03-c-05-image-build",
        "blueprint_id": blueprint["blueprint_id"],
        "deployment_schema_id": deployment["deployment_schema_id"],
        "scenario_id": "S-04",
        "testcase_id": "TC-03",
        "combination_id": "C-05",
        "profile_id": "waffle-jetson-arm64",
        "source_inputs": [],
        "registry": "registry:5000",
        "images": list((images or {}).values()),
    }
    write_response_adapter(
        image_build, {"image-build-result.json": json.dumps(result)}, "succeeded"
    )
    descriptor = repository / STAGES["image_build"].jenkins_descriptor
    committed = descriptor.read_text()
    scripted = committed.replace(
        "entrypoint: sdi-image-build-adapter", f"entrypoint: {image_build}"
    )
    assert scripted != committed
    if images is not None:
        descriptor.write_text(scripted)
    return repository


def script_stage(repository: Path, stage: StageName, entrypoint: Path) -> None:
    descriptor = repository / STAGES[stage].jenkins_descriptor
    committed = descriptor.read_text()
    scripted = re.sub(
        r"entrypoint: .*", f"entrypoint: {entrypoint}", committed
    ).replace("implementation_mode: fixture", "implementation_mode: implemented")
    assert scripted != committed
    descriptor.write_text(scripted)


def select_evidence_adapter(  # noqa: PLR0913, PLR0917
    repository: Path,
    stage: Literal["cv", "cd"],
    domain_outcome: str,
    evidence: dict[str, Any] | None,
    reason: dict[str, str] | None,
    attachment: bytes | None = None,
) -> None:
    (grant,) = STAGES[stage].grant.evidence
    attachments = {
        item.name: item.file_name for item in STAGES[stage].grant.attachments
    }
    adapter = repository.parent / f"scripted-{stage}-adapter"
    write_scripted_adapter(
        adapter,
        f"""evidence = {evidence!r}
attachment = {attachment!r}
attachments = {attachments!r}
produced = []
if evidence is not None:
    evidence["source_inputs"] = [
        {{"slot": item["slot"], "sha256": item["sha256"]}} for item in request["inputs"]
    ]
    (output / "outputs").mkdir()
    (output / "outputs/{grant.file_name}").write_text(json.dumps(evidence))
    produced.append({grant.name!r})
if attachment is not None:
    (output / "outputs").mkdir(exist_ok=True)
    for slot, name in attachments.items():
        (output / "outputs" / name).write_bytes(attachment)
        produced.append(slot)
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": {domain_outcome!r},
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": produced,
    "diagnostic": {{"present": False, "truncated": False}},
}}
reason = {reason!r}
if reason is not None:
    response["reason"] = reason
(output / "response.json").write_text(json.dumps(response))
""",
    )
    script_stage(repository, stage, adapter)


CV_PLACEMENTS = [
    placement | {"status": "resolved", "artifact_id": "debs"}
    for placement in PLACEMENTS
]
CV_IMAGES = {
    service_id: {
        "service_id": service_id,
        "status": "built",
        "repository": f"sdi/{service_id}",
        "digest": DIGEST,
        "platforms": [
            {"architecture": "amd64", "digest": AMD64_DIGEST},
            {"architecture": "arm64", "digest": DIGEST},
        ],
    }
    for service_id in SERVICE_IDS
}
CV_REASONS = {
    "succeeded": None,
    "failed": {
        "category": "domain",
        "code": "sdi.cv.failed",
        "summary": "fixture-navigation did not participate.",
    },
    "not_evaluated": {
        "category": "dependency",
        "code": "sdi.cv.not-evaluated",
        "summary": "fixture-mission-control has an unsupported command.",
    },
}
CD_IMAGES = {
    service_id: {
        "service_id": service_id,
        "status": "built",
        "repository": f"sdi/{service_id}",
        "digest": cd.INDEX_DIGEST,
        "platforms": [
            {"architecture": "amd64", "digest": AMD64_DIGEST},
            {"architecture": "arm64", "digest": cd.ARM64_DIGEST},
        ],
    }
    for service_id in SERVICE_IDS
}
CD_REASONS = {
    "succeeded": None,
    "failed": {
        "category": "domain",
        "code": "sdi.cd.failed",
        "summary": "fixture-navigation is in CrashLoopBackOff.",
    },
    "not_evaluated": {
        "category": "dependency",
        "code": "sdi.cd.not-evaluated",
        "summary": "fixture-navigation has no image for arm64.",
    },
}


def script_adapter(repository: Path, stage: StageName, body: str) -> None:
    adapter = repository.parent / f"{stage}-adapter-body"
    write_scripted_adapter(adapter, body)
    script_stage(repository, stage, adapter)


def mirror_local_overrides(repository: Path) -> None:
    for stage in STAGES.values():
        if stage.local_override is not None:
            (repository / stage.local_override).write_bytes(
                (repository / stage.jenkins_descriptor).read_bytes()
            )


def respond(
    conclusion: str,
    domain_outcome: str,
    reason: dict[str, str] | None = None,
    *,
    documents: dict[str, str] | None = None,
) -> str:
    return f"""documents = {documents or {}!r}
for name, document in documents.items():
    (output / "outputs").mkdir(exist_ok=True)
    (output / "outputs" / name).write_text(document)
response = {{
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": {conclusion!r},
    "domain_outcome": {domain_outcome!r},
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [
        item["slot"] for item in request["outputs"]
        if item["role"] == "domain_output"
        and item["path"].removeprefix("outputs/") in documents
    ],
    "diagnostic": {{"present": False, "truncated": False}},
}}
if {reason!r} is not None:
    response["reason"] = {reason!r}
(output / "response.json").write_text(json.dumps(response))
"""


HANG = "import time\ntime.sleep(600)\n"
EXIT_NONZERO = "raise SystemExit(3)\n"
SIGNAL_SELF = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
LEAVE_DESCENDANT = "import subprocess\nsubprocess.Popen(['sleep', '30'])\n"

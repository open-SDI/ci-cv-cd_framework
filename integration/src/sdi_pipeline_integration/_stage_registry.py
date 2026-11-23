"""The four Stages and the agents that run them, one row each."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type StageName = Literal["composition", "image_build", "cv", "cd"]
type SlotOrigin = Literal["committed", "supplied"] | StageName


@dataclass(frozen=True)
class Slot:
    """One Domain artifact a Stage may be granted."""

    name: str
    file_name: str
    media_type: str
    schema_version: str
    origin: SlotOrigin


SLOTS = (
    Slot(
        "run_request",
        "run-request.yaml",
        "application/yaml",
        "sdi.pipeline-integration-run-request/v1",
        "committed",
    ),
    Slot(
        "requirements_specification",
        "requirements-specification.md",
        "text/markdown",
        "sdi.mobility-requirements-specification/v1",
        "committed",
    ),
    Slot(
        "target_profile",
        "target-profile.yaml",
        "application/yaml",
        "sdi.target-execution-profile/v1",
        "committed",
    ),
    Slot(
        "service_repository",
        "service-repository.json",
        "application/json",
        "sdi.service-repository-manifest/v1",
        "supplied",
    ),
    Slot(
        "composition_settings",
        "composition-settings.json",
        "application/json",
        "sdi.composition-settings/v1",
        "supplied",
    ),
    Slot(
        "composition_blueprint",
        "composition-blueprint.json",
        "application/json",
        "sdi.composition-blueprint/v1",
        "composition",
    ),
    Slot(
        "deployment_schema",
        "deployment-schema.json",
        "application/json",
        "sdi.deployment-schema/v1",
        "composition",
    ),
    Slot(
        "image_build_result",
        "image-build-result.json",
        "application/json",
        "sdi.image-build-result/v1",
        "image_build",
    ),
)
# One overview video of a CV run at 640x480 and 10 fps stays well under this.
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
SLOT_BY_NAME = {slot.name: slot for slot in SLOTS}
SLOT_BY_SCHEMA = {slot.schema_version: slot for slot in SLOTS}
COMMITTED_SLOTS = tuple(slot for slot in SLOTS if slot.origin == "committed")
SUPPLIED_INPUT_SLOTS = frozenset(
    slot.name for slot in SLOTS if slot.origin == "supplied"
)


@dataclass(frozen=True)
class EvidenceGrant:
    """Evidence of how a Stage reached its outcome, which no later Stage reads.

    Required evidence comes from every implemented attempt whose execution
    succeeded.
    """

    name: str
    file_name: str
    media_type: str
    schema_version: str
    max_bytes: int
    required: bool = False


@dataclass(frozen=True)
class AttachmentGrant:
    """A file a Stage may attach for people, such as a video, never parsed.

    Attachments are always optional, and no later Stage reads them.
    """

    name: str
    file_name: str
    media_type: str
    schema_version: str
    max_bytes: int


@dataclass(frozen=True)
class StageGrant:
    """Everything one Stage is granted."""

    profile_version: str
    inputs: tuple[str, ...]
    work_limit_seconds: int
    domain_output_max_bytes: int
    diagnostic_max_bytes: int
    evidence: tuple[EvidenceGrant, ...] = ()
    attachments: tuple[AttachmentGrant, ...] = ()
    minimum_work_limit_seconds: int = 1


@dataclass(frozen=True)
class Agent:
    """One Jenkins inbound agent."""

    node: str
    label: str


@dataclass(frozen=True)
class Credential:
    """One Jenkins string credential a Stage binds."""

    jenkins_id: str
    variable: str


@dataclass(frozen=True)
class Stage:
    """One Stage's grant and wiring."""

    name: StageName
    display_name: str
    event_label: str
    grant: StageGrant
    agent: Agent
    jenkins_descriptor: str
    local_override: str | None = None
    credentials: tuple[Credential, ...] = ()

    @property
    def local_descriptor(self) -> str:
        """The descriptor dispatch-local runs."""
        return self.local_override or self.jenkins_descriptor

    @property
    def event_tag(self) -> str:
        """The `[sdi]` line's Stage column, such as `IMAGE-BUILD`."""
        return self.event_label.upper().replace(" ", "-")


STAGES: dict[StageName, Stage] = {
    "composition": Stage(
        "composition",
        "Composition",
        event_label="Composition",
        grant=StageGrant(
            "sdi.composition-stage-profile/v1",
            (
                "run_request",
                "requirements_specification",
                "target_profile",
                "service_repository",
                "composition_settings",
            ),
            work_limit_seconds=10 * 60,
            domain_output_max_bytes=32768,
            diagnostic_max_bytes=16384,
            evidence=(
                EvidenceGrant(
                    "composition_evidence",
                    "composition-evidence.json",
                    "application/json",
                    "sdi.composition-evidence/v1",
                    max_bytes=262144,
                ),
            ),
        ),
        agent=Agent("ci", "composition"),
        jenkins_descriptor="deployment/jenkins/adapters/composition-v1.yaml",
        credentials=(Credential("sdi-openai-api-key", "OPENAI_API_KEY"),),
    ),
    "image_build": Stage(
        "image_build",
        "Image build",
        event_label="Image build",
        grant=StageGrant(
            "sdi.image-build-stage-profile/v1",
            (
                "target_profile",
                "service_repository",
                "composition_blueprint",
                "deployment_schema",
            ),
            work_limit_seconds=30 * 60,
            domain_output_max_bytes=32768,
            diagnostic_max_bytes=16384,
        ),
        agent=Agent("image-build", "image-build"),
        jenkins_descriptor="deployment/jenkins/adapters/image-build-v1.yaml",
        local_override="deployment/jenkins/adapters/image-build-fixture-v1.yaml",
    ),
    "cv": Stage(
        "cv",
        "Continuous Validation",
        event_label="CV",
        grant=StageGrant(
            "sdi.cv-stage-profile/v1",
            (
                "requirements_specification",
                "target_profile",
                "service_repository",
                "composition_blueprint",
                "deployment_schema",
                "image_build_result",
            ),
            work_limit_seconds=45 * 60,
            domain_output_max_bytes=32768,
            diagnostic_max_bytes=16384,
            evidence=(
                EvidenceGrant(
                    "validation_evidence",
                    "validation-evidence.json",
                    "application/json",
                    "sdi.validation-evidence/v1",
                    max_bytes=32768,
                    required=True,
                ),
            ),
            attachments=(
                AttachmentGrant(
                    "sim_recording",
                    "sim-recording.mp4",
                    "video/mp4",
                    "sdi.sim-recording/v1",
                    max_bytes=MAX_ATTACHMENT_BYTES,
                ),
            ),
        ),
        agent=Agent("cv", "cv"),
        jenkins_descriptor="deployment/jenkins/adapters/cv-v1.yaml",
    ),
    "cd": Stage(
        "cd",
        "Continuous Deployment",
        event_label="CD",
        grant=StageGrant(
            "sdi.cd-stage-profile/v1",
            (
                "target_profile",
                "composition_blueprint",
                "deployment_schema",
                "image_build_result",
            ),
            work_limit_seconds=15 * 60,
            domain_output_max_bytes=32768,
            diagnostic_max_bytes=4096,
            evidence=(
                EvidenceGrant(
                    "deployment_result",
                    "deployment-result.json",
                    "application/json",
                    "sdi.deployment-result/v1",
                    max_bytes=65536,
                    required=True,
                ),
            ),
            # The CD adapter's teardown margin, longest node wait, and a full
            # kubectl call each for create and apply.
            minimum_work_limit_seconds=60 + 120 + 2 * 20,
        ),
        agent=Agent("cd", "cd"),
        jenkins_descriptor="deployment/jenkins/adapters/cd-v1.yaml",
        local_override="deployment/jenkins/adapters/cd-fixture-v1.yaml",
    ),
}
STAGE_ORDER: tuple[StageName, ...] = tuple(STAGES)
INTEGRATION_AGENT = Agent("integration", "integration")
AGENTS: tuple[Agent, ...] = (
    INTEGRATION_AGENT,
    *(stage.agent for stage in STAGES.values()),
)
EXPECTED_STAGE_INPUT_SLOTS: dict[StageName, tuple[str, ...]] = {
    name: stage.grant.inputs for name, stage in STAGES.items()
}
EXPECTED_STAGE_OUTPUTS: dict[StageName, dict[str, tuple[str, str, str]]] = {
    name: {
        slot.name: (f"outputs/{slot.file_name}", slot.media_type, slot.schema_version)
        for slot in SLOTS
        if slot.origin == name
    }
    for name in STAGES
}
EXPECTED_STAGE_EVIDENCE: dict[StageName, dict[str, tuple[str, str, str]]] = {
    name: {
        evidence.name: (
            f"outputs/{evidence.file_name}",
            evidence.media_type,
            evidence.schema_version,
        )
        for evidence in stage.grant.evidence
    }
    for name, stage in STAGES.items()
}

EXPECTED_STAGE_ATTACHMENTS: dict[StageName, dict[str, tuple[str, str, str]]] = {
    name: {
        attachment.name: (
            f"outputs/{attachment.file_name}",
            attachment.media_type,
            attachment.schema_version,
        )
        for attachment in stage.grant.attachments
    }
    for name, stage in STAGES.items()
}


@dataclass(frozen=True)
class RunLimits:
    """One run's wall-clock budget."""

    github_job_seconds: int = 110 * 60
    run_seconds: int = 90 * 60
    finalization_reserve_seconds: int = 2 * 60
    stage_safety_margin_seconds: int = 60


RUN_LIMITS = RunLimits()

"""The pipeline-event vocabulary: statuses, step codes, slots, templates, paths."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Literal, get_args

from ._cv_result import REQUIREMENTS, StepStatus
from ._domain_contracts import FAILED_IMAGE_REASONS, K8S_REASONS, UNEVALUATED_REASONS
from ._stage_registry import STAGE_ORDER, STAGES, StageName

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

type PipelineStatus = Literal[
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED", "NOT_EVALUATED"
]
PIPELINE_STATUSES: tuple[PipelineStatus, ...] = get_args(PipelineStatus.__value__)

type StepKind = Literal["step", "item", "result"]
type Emitter = Literal["runtime", "adapter"]
type StepScope = Literal["run", "every-stage"] | StageName

type StepCode = Literal[
    "run-accepted",
    "run-finished",
    "stage-started",
    "adapter-running",
    "work-limit-reached",
    "run-deadline-reached",
    "checking-output",
    "publishing",
    "stage-finished",
    "discovery",
    "generation",
    "assessment",
    "builder-check",
    "building",
    "image-built",
    "image-failed",
    "screening",
    "service-unfit",
    "leftover-cleanup",
    "sim-cached",
    "sim-build",
    "pull",
    "pull-blocked",
    "start",
    "mission",
    "mission-setup-done",
    "mission-step",
    "collecting-result",
    "teardown",
    "nodes-wait",
    "nodes-ready",
    "applying",
    "pod-running",
    "pod-not-running",
    "settling",
    "judging",
    "cleanup",
]

type SlotName = Literal[
    "Stage",
    "composition_id",
    "profile_id",
    "ci_status",
    "cv_status",
    "cd_status",
    "status",
    "reason",
    "limit_min",
    "n",
    "i",
    "m",
    "s",
    "n_started",
    "n_left",
    "search",
    "provider",
    "service_id",
    "host",
    "image_reason",
    "unfit_reason",
    "pull_reason",
    "setup_status",
    "mission_step",
    "mission_status",
    "k8s_reason",
]
type SlotValue = int | str

type SearchMode = Literal["lexical", "lexical+embeddings"]
type PullReason = Literal["image-unavailable", "unsupported-command"]
type SetupStatus = Literal["passed", "timed_out"]
UNFIT_REASONS = tuple(
    reason
    for reason in UNEVALUATED_REASONS
    if reason not in {"image-unavailable", "blocked"}
)
MISSION_STATUSES = tuple(
    status for status in get_args(StepStatus) if status != "not_reached"
)

INTEGER_MAX_DIGITS = 10


@dataclass(frozen=True)
class SlotType:
    """How one slot's value is checked and sized."""

    kind: Literal["integer", "enum", "identifier"]
    max_length: int
    values: tuple[str, ...] = ()
    pattern: str = ""

    @property
    def optional(self) -> bool:
        """Whether the short form of a message leaves this slot out."""
        return self.kind == "identifier"

    @property
    def value_regex(self) -> str:
        """The unanchored regex a rendered value of this slot matches."""
        match self.kind:
            case "integer":
                return f"[0-9]{{1,{self.max_length}}}"
            case "enum":
                ordered = sorted(self.values, key=len, reverse=True)
                return "(?:" + "|".join(map(re.escape, ordered)) + ")"
            case "identifier":
                return self.pattern


def _enum(values: tuple[str, ...]) -> SlotType:
    return SlotType("enum", max(map(len, values)), values=values)


_INTEGER = SlotType("integer", INTEGER_MAX_DIGITS)
_SLUG = SlotType("identifier", 63, pattern=r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_STATUS = _enum(PIPELINE_STATUSES)

SLOT_TYPES: dict[SlotName, SlotType] = {
    "Stage": _enum(tuple(STAGES[name].event_label for name in STAGE_ORDER)),
    "composition_id": SlotType("identifier", 4, pattern=r"C-[0-9]{2}"),
    "profile_id": _SLUG,
    "ci_status": _STATUS,
    "cv_status": _STATUS,
    "cd_status": _STATUS,
    "status": _STATUS,
    "reason": SlotType(
        "identifier", 127, pattern=r"sdi\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+"
    ),
    "limit_min": _INTEGER,
    "n": _INTEGER,
    "i": _INTEGER,
    "m": _INTEGER,
    "s": _INTEGER,
    "n_started": _INTEGER,
    "n_left": _INTEGER,
    "search": _enum(get_args(SearchMode.__value__)),
    "provider": _SLUG,
    "service_id": _SLUG,
    "host": _SLUG,
    "image_reason": _enum(FAILED_IMAGE_REASONS),
    "unfit_reason": _enum(UNFIT_REASONS),
    "pull_reason": _enum(get_args(PullReason.__value__)),
    "setup_status": _enum(get_args(SetupStatus.__value__)),
    "mission_step": _enum(REQUIREMENTS),
    "mission_status": _enum(MISSION_STATUSES),
    "k8s_reason": _enum(K8S_REASONS),
}


def template_slots(template: str) -> frozenset[SlotName]:
    """Return the slot names a template refers to, read from its `{name}` fields."""
    names = {
        field
        for _, field, _, _ in string.Formatter().parse(template)
        if field is not None
    }
    unknown = names - SLOT_TYPES.keys()
    if unknown:
        msg = f"template {template!r} names untyped slots {sorted(unknown)}"
        raise ValueError(msg)
    return frozenset(name for name in SLOT_TYPES if name in names)


def takes_slots(template: str, slot_names: AbstractSet[SlotName]) -> bool:
    """Whether `template` takes exactly `slot_names`, besides `{Stage}`."""
    return template_slots(template) - {"Stage"} == slot_names


@dataclass(frozen=True)
class Step:
    """One catalogue row."""

    code: StepCode
    scope: StepScope
    kind: StepKind
    forms: tuple[str, ...]
    heartbeat: str | None = None

    @property
    def emitter(self) -> Emitter:
        """The runtime prints run-level and every-Stage steps; adapters the rest."""
        return "runtime" if self.scope in {"run", "every-stage"} else "adapter"

    @cached_property
    def form_slots(self) -> tuple[frozenset[SlotName], ...]:
        """Each form's slot set, in `forms` order."""
        return tuple(template_slots(form) for form in self.forms)

    def form_for(self, slot_names: AbstractSet[SlotName]) -> str | None:
        """Return the form the caller's slots pick, or None if none takes them."""
        return next(
            (form for form in self.forms if takes_slots(form, slot_names)), None
        )

    def allows_stage(self, stage: StageName | None) -> bool:
        """Whether a line with this `stage` field may carry this step."""
        match self.scope:
            case "run":
                return stage is None
            case "every-stage":
                return stage is not None
            case _:
                return stage == self.scope


STEPS: tuple[Step, ...] = (
    Step("run-accepted", "run", "step",
         ("Run accepted for {composition_id} on {profile_id}",)),
    Step("run-finished", "run", "step",
         ("Final: CI {ci_status}, CV {cv_status}, CD {cd_status}",)),
    Step("stage-started", "every-stage", "step", ("{Stage} started",)),
    Step("adapter-running", "every-stage", "step",
         ("{Stage} work started (limit {limit_min} min)",)),
    Step("work-limit-reached", "every-stage", "step",
         ("{Stage} hit its {limit_min} min limit, stopping",)),
    Step("run-deadline-reached", "every-stage", "step",
         ("Run time limit reached, stopping {Stage}",)),
    Step("checking-output", "every-stage", "step", ("Checking {Stage} output",)),
    Step("publishing", "every-stage", "step", ("Recording {Stage} result",)),
    Step("stage-finished", "every-stage", "step",
         ("{Stage} finished: {status} ({reason})", "{Stage} finished: {status}")),
    Step("discovery", "composition", "step",
         ("Finding candidate services ({n} in repository, {search})",)),
    Step("generation", "composition", "step",
         ("Asking {provider} for proposals",),
         heartbeat="Still waiting for {provider}, {m} min"),
    Step("assessment", "composition", "step", ("Assessing {n} proposals",)),
    Step("builder-check", "image_build", "step", ("Checking builder and registry",)),
    Step("building", "image_build", "step",
         ("Building {n_started} images (amd64+arm64)",),
         heartbeat="Still building {n_left} images, {m} min"),
    Step("image-built", "image_build", "item", ("{service_id} built ({i}/{n})",)),
    Step("image-failed", "image_build", "item",
         ("{service_id} failed: {image_reason} ({i}/{n})",)),
    Step("screening", "cv", "step",
         ("Checking {n} services can run in simulation",)),
    Step("service-unfit", "cv", "item",
         ("{service_id} cannot run in simulation: {unfit_reason}",)),
    Step("leftover-cleanup", "cv", "step", ("Removing leftover CV containers",)),
    Step("sim-cached", "cv", "step", ("Simulator image cached",)),
    Step("sim-build", "cv", "step",
         ("Building simulator image (cold, can take long)",),
         heartbeat="Still building simulator image, {m} min"),
    Step("pull", "cv", "item", ("Pulled {service_id} ({i}/{n})",)),
    Step("pull-blocked", "cv", "item",
         ("{service_id} blocked: {pull_reason} ({i}/{n})",)),
    Step("start", "cv", "step",
         ("Starting simulator, {n} services and orchestrator",)),
    Step("mission", "cv", "step",
         ("Mission setup (face, localization, navigation)",),
         heartbeat="Mission still running, {m} min"),
    Step("mission-setup-done", "cv", "result",
         ("Setup {setup_status} after {s} s",)),
    Step("mission-step", "cv", "item",
         ("Step {mission_step}: {mission_status} after {s} s ({i}/{n})",
          "Step {mission_step}: scripted ({i}/{n})")),
    Step("collecting-result", "cv", "step", ("Reading the mission result",)),
    Step("teardown", "cv", "step", ("Removing CV containers",)),
    Step("nodes-wait", "cd", "step", ("Waiting for {n} hosts to be Ready",)),
    Step("nodes-ready", "cd", "result", ("Hosts Ready after {s} s",)),
    Step("applying", "cd", "step", ("Deploying {n} services",)),
    Step("pod-running", "cd", "item",
         ("{service_id} on {host} Running after {s} s ({i}/{n})",)),
    Step("pod-not-running", "cd", "item",
         ("{service_id} on {host}: {k8s_reason} ({i}/{n})",)),
    Step("settling", "cd", "step", ("All pods settled; holding {s} s",)),
    Step("judging", "cd", "step", ("Checking placements",)),
    Step("cleanup", "cd", "step", ("Removing deployment namespace",)),
)  # fmt: skip
STEP_BY_CODE: dict[StepCode, Step] = {step.code: step for step in STEPS}
STEP_CODES: tuple[StepCode, ...] = tuple(STEP_BY_CODE)

type StagePath = tuple[StepCode, ...]
type StageOutcome = Literal[
    "normal",
    "adapter-not-started",
    "adapter-failed",
    "work-limit",
    "run-deadline",
    "pre-spawn-skip",
]
STAGE_PATHS: dict[StageOutcome, StagePath] = {
    # Rejected output takes the normal path too.
    "normal": ("stage-started", "adapter-running", "checking-output", "publishing",
               "stage-finished"),
    "adapter-not-started": ("stage-started", "publishing", "stage-finished"),
    # A nonzero exit, a signal, or surviving descendants.
    "adapter-failed": ("stage-started", "adapter-running", "publishing",
                       "stage-finished"),
    "work-limit": ("stage-started", "adapter-running", "work-limit-reached",
                   "publishing", "stage-finished"),
    "run-deadline": ("stage-started", "adapter-running", "run-deadline-reached",
                     "publishing", "stage-finished"),
    "pre-spawn-skip": ("stage-finished",),
}  # fmt: skip
KILLED_PREFIXES: frozenset[StagePath] = frozenset(
    path[:end] for path in STAGE_PATHS.values() for end in range(1, len(path))
)


def longest_sdi_prefix_bytes() -> int:
    """Return the length of the longest `[sdi]` prefix of a Stage-level line."""
    longest_tag = max(len(STAGES[name].event_tag) for name in STAGE_ORDER)
    longest_code = max(map(len, STEP_CODES))
    longest_status = max(map(len, PIPELINE_STATUSES))
    return (
        len("[sdi] C-00 ") + longest_tag + len(" > ") + longest_code + 2
        + longest_status + 2
    )  # fmt: skip


SDI_LINE_MAX_BYTES = 1024
MESSAGE_MAX_BYTES = SDI_LINE_MAX_BYTES - longest_sdi_prefix_bytes()

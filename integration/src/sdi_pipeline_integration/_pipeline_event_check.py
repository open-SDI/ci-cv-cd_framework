"""The pipeline-event conformance checker and the golden file format."""

from __future__ import annotations

import functools
import json
import re
import string
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, cast

from pydantic import JsonValue, ValidationError

from ._pipeline_event_catalogue import (
    KILLED_PREFIXES,
    PIPELINE_STATUSES,
    SDI_LINE_MAX_BYTES,
    SLOT_TYPES,
    STAGE_PATHS,
    STEPS,
    PipelineStatus,
    SlotName,
    Step,
)
from ._pipeline_events import (
    PRISM_PREFIX,
    SDI_PREFIX,
    PipelineEventRow,
    derive_ci_status,
    render_prism_line,
    render_sdi_line,
)
from ._stage_registry import STAGE_ORDER, STAGES, StageName
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from ._pipeline_events import EventScope

type Rule = Literal[
    "parse",
    "pairing",
    "characters",
    "size",
    "sdi-line",
    "prism-line",
    "status-column",
    "stage-status",
    "identity",
    "final",
    "closed-stage",
    "succeeded-origin",
    "succeeded-stable",
    "catalogue",
    "message",
    "ci-status",
    "path",
    "final-statuses",
    "decorated",
    "golden",
    "extra",
]
type StatusVector = tuple[
    PipelineStatus, PipelineStatus, PipelineStatus, PipelineStatus
]

STATUS_LETTERS: dict[PipelineStatus, str] = dict(
    zip(PIPELINE_STATUSES, ("P", "R", "S", "F", "K", "N"), strict=True)
)
LETTER_STATUSES: dict[str, PipelineStatus] = {
    letter: status for status, letter in STATUS_LETTERS.items()
}
TERMINAL_STATUSES: frozenset[PipelineStatus] = frozenset(
    {"SUCCEEDED", "FAILED", "SKIPPED", "NOT_EVALUATED"}
)
RUN_DEADLINE_REASON = "sdi.run.deadline-exceeded"
DROPPED_REPORTS_PREFIX = b"sdi-integration: dropped "
IDENTITY_FIELDS = (
    "execution_id",
    "composition_id",
    "scenario_id",
    "testcase_id",
    "profile_id",
)
_STEPS: dict[str, Step] = {step.code: step for step in STEPS}


@dataclass(frozen=True)
class Violation:
    """One broken rule."""

    rule: Rule
    detail: str
    line: int | None = None

    def render(self, file_name: str) -> str:
        """Return `<file>:<line>: [<rule>] <detail>`, as `check-events` prints it."""
        where = file_name if self.line is None else f"{file_name}:{self.line}"
        return f"{where}: [{self.rule}] {self.detail}"


def _four(statuses: list[PipelineStatus]) -> StatusVector:
    first, second, third, fourth = statuses
    return (first, second, third, fourth)


def status_vector(row: PipelineEventRow) -> StatusVector:
    """Return the four Stage statuses of a row, in Stage order."""
    return (
        row.composition_status,
        row.image_build_status,
        row.cv_status,
        row.cd_status,
    )


@dataclass(frozen=True)
class Projection:
    """One line in the golden projection form."""

    stage: StageName | None
    step: str
    final: bool
    statuses: StatusVector
    failure_reason: str | None

    @classmethod
    def of(cls, row: PipelineEventRow) -> Projection:
        """Project a parsed row."""
        return cls(
            row.stage, row.step, row.final, status_vector(row), row.failure_reason
        )

    @classmethod
    def parse(cls, text: str) -> Projection:
        """Parse a projection line; raise InputError when it is malformed."""
        match = _PROJECTION.fullmatch(text)
        if match is None:
            msg = f"not a projection line: {text!r}"
            raise InputError(msg)
        stage_text, step_code, final, letters, reason = match.groups()
        stage = None if stage_text == "RUN" else cast("StageName", stage_text)
        step = _STEPS.get(step_code)
        if step is None or step.emitter != "runtime" or not step.allows_stage(stage):
            msg = f"{step_code!r} is not a runtime step of {stage_text}"
            raise InputError(msg)
        return cls(
            stage,
            step_code,
            final is not None,
            _four([LETTER_STATUSES[letter] for letter in letters.split()]),
            None if reason == "-" else reason,
        )

    def __str__(self) -> str:
        """Return the projection as a golden file writes it."""
        letters = " ".join(STATUS_LETTERS[status] for status in self.statuses)
        final = " final" if self.final else ""
        return (
            f"{self.stage or 'RUN'} {self.step}{final} [{letters}] "
            f"{self.failure_reason or '-'}"
        )


_PROJECTION = re.compile(
    r"(RUN|" + "|".join(STAGE_ORDER) + r") ([a-z-]+)( final)? "
    r"\[([PRSFKN](?: [PRSFKN]){3})\] (\S+)"
)


@dataclass(frozen=True)
class Equals:
    """`<field> == <json>`."""

    field: str
    value: JsonValue

    def __str__(self) -> str:
        """Return the assertion as a golden file writes it."""
        return f"{self.field} == {json.dumps(self.value)}"


@dataclass(frozen=True)
class IsSet:
    """`<field> != null`."""

    field: str

    def __str__(self) -> str:
        """Return the assertion as a golden file writes it."""
        return f"{self.field} != null"


@dataclass(frozen=True)
class EqualsField:
    """`<field> == @<other>`, or `<field> == @<other> - <minus>`."""

    field: str
    other: str
    minus: int = 0

    def __str__(self) -> str:
        """Return the assertion as a golden file writes it."""
        minus = f" - {self.minus}" if self.minus else ""
        return f"{self.field} == @{self.other}{minus}"


@dataclass(frozen=True)
class Contains:
    """`<field> contains <json object>`."""

    field: str
    subset: dict[str, JsonValue]

    def __str__(self) -> str:
        """Return the assertion as a golden file writes it."""
        return f"{self.field} contains {json.dumps(self.subset)}"


@dataclass(frozen=True)
class EveryKeySet:
    """`<field>[*].<key> != null`."""

    field: str
    key: str

    def __str__(self) -> str:
        """Return the assertion as a golden file writes it."""
        return f"{self.field}[*].{self.key} != null"


type Assertion = Equals | IsSet | EqualsField | Contains | EveryKeySet

_EVERY_KEY_SET = re.compile(r"(\w+)\[\*\]\.(\w+) != null")
_IS_SET = re.compile(r"(\w+) != null")
_EQUALS_FIELD = re.compile(r"(\w+) == @(\w+)(?: - ([0-9]+))?")
_CONTAINS = re.compile(r"(\w+) contains (\{.*\})")
_EQUALS = re.compile(r"(\w+) == (.+)")


def _row_field(name: str) -> str:
    if name not in PipelineEventRow.model_fields:
        msg = f"{name!r} is not a pipeline-event field"
        raise InputError(msg)
    return name


def _json(text: str) -> JsonValue:
    try:
        return cast("JsonValue", json.loads(text))
    except json.JSONDecodeError as error:
        msg = f"{text!r} is not JSON: {error}"
        raise InputError(msg) from None


def parse_assertion(text: str) -> Assertion:
    """Parse one indented golden line; raise InputError naming what is wrong."""
    if match := _EVERY_KEY_SET.fullmatch(text):
        return EveryKeySet(_row_field(match[1]), match[2])
    if match := _IS_SET.fullmatch(text):
        return IsSet(_row_field(match[1]))
    if match := _EQUALS_FIELD.fullmatch(text):
        return EqualsField(
            _row_field(match[1]), _row_field(match[2]), int(match[3] or 0)
        )
    if match := _CONTAINS.fullmatch(text):
        subset = _json(match[2])
        if not isinstance(subset, dict):
            msg = f"{text!r} needs a JSON object"
            raise InputError(msg)
        return Contains(_row_field(match[1]), subset)
    if match := _EQUALS.fullmatch(text):
        return Equals(_row_field(match[1]), _json(match[2]))
    msg = f"not an assertion: {text!r}"
    raise InputError(msg)


def assertion_holds(assertion: Assertion, row: Mapping[str, JsonValue]) -> bool:
    """Evaluate one assertion against a row's JSON fields."""
    match assertion:
        case Equals(name, value):
            return row[name] == value
        case IsSet(name):
            return row[name] is not None
        case EqualsField(name, other, minus):
            actual, base = row[name], row[other]
            if not minus:
                return actual == base
            return (
                isinstance(actual, int)
                and isinstance(base, int)
                and (actual == base - minus)
            )
        case Contains(name, subset):
            items = row[name]
            return isinstance(items, list) and any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in subset.items())
                for item in items
            )
        case EveryKeySet(name, key):
            items = row[name]
            return isinstance(items, list) and all(
                isinstance(item, dict) and item.get(key) is not None for item in items
            )


@dataclass(frozen=True)
class GoldenLine:
    """One projection and the assertions listed under it."""

    projection: Projection
    assertions: tuple[Assertion, ...]
    source_line: int


@dataclass(frozen=True)
class Golden:
    """A parsed golden file, named by its file stem."""

    name: str
    lines: tuple[GoldenLine, ...]


def load_golden(path: Path) -> Golden:
    """Read a golden file; raise InputError on a malformed one."""
    lines: list[GoldenLine] = []
    for number, text in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        if not text.strip() or text.startswith("#"):
            continue
        if text[0] in " \t" and not lines:
            msg = f"{path}:{number}: an assertion before any projection line"
            raise InputError(msg)
        try:
            if text[0] in " \t":
                last = lines[-1]
                assertion = parse_assertion(text.strip())
                lines[-1] = replace(last, assertions=(*last.assertions, assertion))
            else:
                lines.append(GoldenLine(Projection.parse(text), (), number))
        except InputError as error:
            msg = f"{path}:{number}: {error}"
            raise InputError(msg) from None
    return Golden(path.stem, tuple(lines))


@dataclass(frozen=True)
class ConsoleEvent:
    """A parsed event."""

    prism_line: int
    row: PipelineEventRow


@dataclass(frozen=True)
class DroppedReports:
    """A plain `sdi-integration: dropped N progress reports` line."""

    line: int


type ConsoleItem = ConsoleEvent | DroppedReports


def _character_problem(line: bytes) -> str | None:
    for column, byte in enumerate(line, 1):
        if not 0x20 <= byte <= 0x7E:  # noqa: PLR2004
            return f"byte 0x{byte:02x} at column {column}"
    if b"##[" in line:
        return "a raw ##["
    return None


def _line_problems(
    number: int, line: bytes, kind: str, max_bytes: int | None = None
) -> list[Violation]:
    problems: list[Violation] = []
    if (character := _character_problem(line)) is not None:
        problems.append(Violation("characters", f"{kind} line has {character}", number))
    if max_bytes is not None and len(line) > max_bytes:
        detail = f"{kind} line is {len(line)} bytes, over {max_bytes}"
        problems.append(Violation("size", detail, number))
    return problems


def _pair_problems(
    sdi: tuple[int, bytes] | None, number: int, prism: bytes, row: PipelineEventRow
) -> list[Violation]:
    problems: list[Violation] = []
    if prism.decode("ascii") != render_prism_line(row):
        detail = "PRISM_EVENT line is not the compact, escaped rendering of its row"
        problems.append(Violation("prism-line", detail, number))
    if sdi is None:
        return problems
    sdi_number, sdi_line = sdi
    expected = render_sdi_line(row)
    if sdi_line.decode("ascii") == expected:
        return problems
    if row.stage is not None and (
        f" > {row.step}  {row.stage_status(row.stage)}  ".encode() not in sdi_line
    ):
        detail = (
            f"[sdi] status column is not {row.stage}'s status on its PRISM_EVENT line"
        )
        problems.append(Violation("status-column", detail, sdi_number))
    else:
        detail = f"[sdi] line is not its PRISM_EVENT line's rendering: {expected!r}"
        problems.append(Violation("sdi-line", detail, sdi_number))
    return problems


def read_console(data: bytes) -> tuple[list[ConsoleItem], list[Violation]]:
    """Split console bytes into events and the noise lines the rules need."""
    items: list[ConsoleItem] = []
    violations: list[Violation] = []
    lines = data.split(b"\n")
    if lines[-1] != b"":
        lines.append(b"")
    held: tuple[int, bytes] | None = None
    for number, line in enumerate(lines, 1):
        if held is not None and not line.startswith(PRISM_PREFIX.encode()):
            detail = "[sdi] line not directly followed by its PRISM_EVENT line"
            violations.append(Violation("pairing", detail, held[0]))
            held = None
        if line.startswith(SDI_PREFIX.encode()):
            held = (number, line)
        elif line.startswith(PRISM_PREFIX.encode()):
            sdi, held = held, None
            if sdi is None:
                detail = "PRISM_EVENT line without its [sdi] line directly before it"
                violations.append(Violation("pairing", detail, number))
            event = _read_event(sdi, number, line, violations)
            if event is not None:
                items.append(event)
        elif line.startswith(DROPPED_REPORTS_PREFIX):
            items.append(DroppedReports(number))
        elif b"PRISM_EVENT {" in line:
            detail = "PRISM_EVENT line does not start at column 0"
            violations.append(Violation("decorated", detail, number))
    return items, violations


def _read_event(
    sdi: tuple[int, bytes] | None,
    number: int,
    line: bytes,
    violations: list[Violation],
) -> ConsoleEvent | None:
    problems = _line_problems(number, line, "PRISM_EVENT")
    if sdi is not None:
        problems += _line_problems(*sdi, "[sdi]", SDI_LINE_MAX_BYTES)
    violations.extend(problems)
    try:
        row = PipelineEventRow.model_validate_json(line[len(PRISM_PREFIX) :])
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(map(str, first["loc"])) or "line"
        detail = f"PRISM_EVENT line does not parse: {location}: {first['msg']}"
        violations.append(Violation("parse", detail, number))
        return None
    if not problems:
        violations.extend(_pair_problems(sdi, number, line, row))
    return ConsoleEvent(number, row)


@functools.cache
def _message_patterns(
    step: Step, stage: StageName | None
) -> tuple[re.Pattern[str], ...]:
    templates = (*step.forms, *filter(None, (step.heartbeat,)))
    patterns: list[re.Pattern[str]] = []
    for template in templates:
        parts: list[str] = []
        for literal, name, _, _ in string.Formatter().parse(template):
            parts.append(re.escape(literal))
            if name is None:
                continue
            slot = cast("SlotName", name)
            value = (
                re.escape(STAGES[stage].event_label)
                if slot == "Stage" and stage is not None
                else SLOT_TYPES[slot].value_regex
            )
            parts.append(f"(?:{value}|-)")
        patterns.append(re.compile("".join(parts)))
    return tuple(patterns)


def _line_rules(events: list[ConsoleEvent], scope: EventScope) -> list[Violation]:
    violations: list[Violation] = []
    for event in events:
        row = event.row
        step = _STEPS.get(row.step)
        if step is None or not step.allows_stage(row.stage):
            detail = f"{row.step!r} is not a catalogue step of {row.stage or 'RUN'}"
            violations.append(Violation("catalogue", detail, event.prism_line))
            continue
        if row.final != (row.step == "run-finished"):
            detail = "final is true exactly on the run-finished line"
            violations.append(Violation("catalogue", detail, event.prism_line))
        if not any(
            pattern.fullmatch(row.message)
            for pattern in _message_patterns(step, row.stage)
        ):
            detail = f"{row.message!r} fits no {row.step} template"
            violations.append(Violation("message", detail, event.prism_line))
        if row.stage is not None and step.emitter == "runtime":
            status = row.stage_status(row.stage)
            allowed = TERMINAL_STATUSES if row.step == "stage-finished" else {"RUNNING"}
            if status not in allowed:
                detail = f"{row.stage} is {status} on its own {row.step} line"
                violations.append(Violation("stage-status", detail, event.prism_line))
        expected = derive_ci_status(
            row.composition_status, row.image_build_status, scope=scope
        )
        if row.ci_status != expected:
            detail = f"ci_status is {row.ci_status}, the CI halves give {expected}"
            violations.append(Violation("ci-status", detail, event.prism_line))
    return violations


def _sequence_rules(events: list[ConsoleEvent]) -> list[Violation]:
    violations: list[Violation] = []
    first = events[0].row
    finished: set[StageName] = set()
    succeeded: set[StageName] = set()
    reported: set[tuple[Rule, StageName]] = set()
    for event in events:
        row = event.row
        changed = [
            name
            for name in IDENTITY_FIELDS
            if getattr(row, name) != getattr(first, name)
        ]
        if changed:
            detail = f"{', '.join(changed)} differ from the first event line"
            violations.append(Violation("identity", detail, event.prism_line))
        if row.final and event is not events[-1]:
            detail = "a final line that is not the last event line"
            violations.append(Violation("final", detail, event.prism_line))
        if row.stage in finished:
            detail = f"a {row.stage} line after {row.stage}'s stage-finished"
            violations.append(Violation("closed-stage", detail, event.prism_line))
        for problem, stage in _succeeded_problems(event, succeeded):
            if (problem.rule, stage) not in reported:
                reported.add((problem.rule, stage))
                violations.append(problem)
        if row.stage is not None and row.step == "stage-finished":
            finished.add(row.stage)
    return violations


def _succeeded_problems(
    event: ConsoleEvent, succeeded: set[StageName]
) -> list[tuple[Violation, StageName]]:
    row = event.row
    problems: list[tuple[Violation, StageName]] = []
    for stage in STAGE_ORDER:
        status = row.stage_status(stage)
        own_finish = row.stage == stage and row.step == "stage-finished"
        if stage in succeeded:
            if status != "SUCCEEDED" and not row.final:
                detail = f"{stage} changed from SUCCEEDED to {status} before the end"
                problems.append(
                    (Violation("succeeded-stable", detail, event.prism_line), stage)
                )
        elif status == "SUCCEEDED":
            if own_finish:
                succeeded.add(stage)
            elif not row.final:
                detail = f"{stage} reads SUCCEEDED before its own stage-finished"
                problems.append(
                    (Violation("succeeded-origin", detail, event.prism_line), stage)
                )
    return problems


@dataclass
class _StageGroup:
    stage: StageName
    line: int
    steps: list[str] = field(default_factory=list[str])


def _path_rules(items: list[ConsoleItem], *, has_run_level: bool) -> list[Violation]:
    groups, violations = _stage_groups(items)
    for index, group in enumerate(groups):
        path = tuple(group.steps)
        killed = index == len(groups) - 1 and path in KILLED_PREFIXES
        if path not in STAGE_PATHS.values() and not killed:
            detail = (
                f"{group.stage} runtime steps {' '.join(path)} follow no Stage path"
            )
            violations.append(Violation("path", detail, group.line))
    if not has_run_level and len(groups) > 1:
        detail = "a console with no run-level line holds one Stage's lines"
        violations.append(Violation("path", detail, groups[1].line))
    return violations


def _stage_groups(
    items: list[ConsoleItem],
) -> tuple[list[_StageGroup], list[Violation]]:
    groups: list[_StageGroup] = []
    violations: list[Violation] = []
    accepted = False
    for item in items:
        if isinstance(item, DroppedReports):
            if not groups or not _accepts_dropped_line(groups[-1].steps):
                detail = "dropped-reports line outside stage-started..publishing"
                violations.append(Violation("path", detail, item.line))
            continue
        row = item.row
        step = _STEPS.get(row.step)
        if step is None or not step.allows_stage(row.stage):
            continue
        if row.stage is not None:
            violations.extend(_add_to_group(groups, row.stage, step, item.prism_line))
        elif row.step == "run-accepted":
            if accepted or groups:
                detail = "run-accepted comes once, before every Stage line"
                violations.append(Violation("path", detail, item.prism_line))
            accepted = True
    return groups, violations


def _add_to_group(
    groups: list[_StageGroup], stage: StageName, step: Step, line: int
) -> list[Violation]:
    problems: list[Violation] = []
    if not groups or groups[-1].stage != stage:
        if groups and STAGE_ORDER.index(stage) <= STAGE_ORDER.index(groups[-1].stage):
            detail = f"{stage} lines after {groups[-1].stage}'s"
            problems.append(Violation("path", detail, line))
        groups.append(_StageGroup(stage, line))
    group = groups[-1]
    if step.emitter == "runtime":
        group.steps.append(step.code)
    elif not group.steps or group.steps[-1] != "adapter-running":
        detail = f"adapter step {step.code} outside {stage}'s adapter-running"
        problems.append(Violation("path", detail, line))
    return problems


def _accepts_dropped_line(steps: list[str]) -> bool:
    return (
        bool(steps)
        and steps[0] == "stage-started"
        and "publishing" not in steps
        and "stage-finished" not in steps
    )


# Spec 5.4 and the G9 race (9.2), not the literal 9.4 wording;
# see docs/pipeline-events.md.
def allowed_final_statuses(
    last_finished: StatusVector,
    final_reason: str | None,
    last_finished_stage: StageName | None,
) -> frozenset[StatusVector]:
    """Return the statuses a final line may show."""
    allowed = {last_finished}
    if final_reason != RUN_DEADLINE_REASON:
        return frozenset(allowed)
    for cut in (
        _first_pending_stage(last_finished),
        _race_stage(last_finished_stage),
    ):
        if cut is None:
            continue
        later_stages = len(STAGE_ORDER) - cut - 1
        skipped: list[PipelineStatus] = ["SKIPPED"] * later_stages
        allowed.add(_four([*last_finished[:cut], "FAILED", *skipped]))
    return frozenset(allowed)


def _first_pending_stage(last_finished: StatusVector) -> int | None:
    """Return the index of the first Stage still PENDING on the last stage-finished."""
    return last_finished.index("PENDING") if "PENDING" in last_finished else None


def _race_stage(last_finished_stage: StageName | None) -> int | None:
    """Return the index of the last stage-finished line's Stage (the G9 race)."""
    if last_finished_stage is None:
        return None
    return STAGE_ORDER.index(last_finished_stage)


def _final_statuses_rule(events: list[ConsoleEvent]) -> list[Violation]:
    final = next((event for event in events if event.row.final), None)
    if final is None:
        return []
    last_finished = _four(["PENDING"] * 4)
    last_stage: StageName | None = None
    for event in events:
        if event is final:
            break
        if event.row.step == "stage-finished" and event.row.stage is not None:
            last_finished, last_stage = status_vector(event.row), event.row.stage
    statuses = status_vector(final.row)
    allowed = allowed_final_statuses(
        last_finished, final.row.failure_reason, last_stage
    )
    if statuses in allowed:
        return []
    shown = " ".join(STATUS_LETTERS[status] for status in statuses)
    last = " ".join(STATUS_LETTERS[status] for status in last_finished)
    detail = f"final statuses [{shown}] do not follow the last stage-finished [{last}]"
    return [Violation("final-statuses", detail, final.prism_line)]


def _golden_rules(events: list[ConsoleEvent], golden: Golden) -> list[Violation]:
    runtime = [
        event
        for event in events
        if (step := _STEPS.get(event.row.step)) is None or step.emitter == "runtime"
    ]
    violations: list[Violation] = []
    for event, expected in zip(runtime, golden.lines, strict=False):
        actual = Projection.of(event.row)
        if actual != expected.projection:
            detail = (
                f"expected `{expected.projection}` ({golden.name} line "
                f"{expected.source_line}), got `{actual}`"
            )
            return [*violations, Violation("golden", detail, event.prism_line)]
        fields = event.row.model_dump(mode="json")
        for assertion in expected.assertions:
            if not assertion_holds(assertion, fields):
                detail = (
                    f"`{assertion}` fails under `{expected.projection}`; "
                    f"{assertion.field} is {json.dumps(fields[assertion.field])}"
                )
                violations.append(Violation("extra", detail, event.prism_line))
    if len(runtime) > len(golden.lines):
        extra = runtime[len(golden.lines)]
        detail = f"`{Projection.of(extra.row)}` after {golden.name}'s last line"
        violations.append(Violation("golden", detail, extra.prism_line))
    elif len(runtime) < len(golden.lines):
        missing = golden.lines[len(runtime)]
        detail = (
            f"missing `{missing.projection}` ({golden.name} line "
            f"{missing.source_line}) and every golden line after it"
        )
        violations.append(Violation("golden", detail))
    return violations


def check_events(console: bytes, golden: Golden | None) -> list[Violation]:
    """Return every violation in a console, in line order; empty means it conforms."""
    items, violations = read_console(console)
    events = [item for item in items if isinstance(item, ConsoleEvent)]
    if events:
        has_run_level = any(event.row.stage is None for event in events)
        scope: EventScope = "run" if has_run_level else "single_stage"
        violations += _line_rules(events, scope)
        violations += _sequence_rules(events)
        violations += _path_rules(items, has_run_level=has_run_level)
        violations += _final_statuses_rule(events)
    if golden is not None:
        violations += _golden_rules(events, golden)
    return sorted(
        violations,
        key=lambda violation: (violation.line is None, violation.line or 0),
    )

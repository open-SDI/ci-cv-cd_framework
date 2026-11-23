from __future__ import annotations

from typing import TYPE_CHECKING

from ._adapter_kit import Outcome, block_cost
from ._cv_run import ContainerState, Finished, Recording, RecordingFailed
from ._cv_verdict import MACHINERY_NODES, CvError, Judgement, topic_constant_drift
from ._domain_contracts import (
    NonParticipatingService,
    UnevaluatedService,
    validation_verdict,
)
from ._image_build_redaction import redact_line, tail_lines

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._adapter_kit import Diagnostic, Succeeded
    from ._cv_result import GraphTopic, ScenarioResult
    from ._cv_run import RunOutcome
    from ._domain_contracts import ServiceParticipation

GRAPH_MAX_BYTES = 8192
EVERY_NODE_TOPICS = frozenset({"/rosout", "/parameter_events"})
UNRECORDED = "unrecorded"


def describe(diagnostic: Diagnostic, result: Judgement | CvError) -> None:
    outcome = result.outcome
    if isinstance(outcome, Outcome):
        diagnostic.line(f"summary: {outcome.summary}")
    if isinstance(result, CvError):
        diagnostic.line(f"error: {result.code}: {_safe(result.detail)}")
    run = result.run
    _run_lines(diagnostic, run)
    scenario = run.result if isinstance(run, Finished) else None
    if scenario is not None:
        _mission_lines(diagnostic, scenario)
    if isinstance(result, Judgement):
        for item in result.services:
            diagnostic.line(_service_line(item))
        if scenario is not None:
            _count_lines(diagnostic, result, scenario)
    if scenario is not None:
        _graph_section(diagnostic, scenario)
    _tails(diagnostic, outcome, result)


def graph_lines(graph: Sequence[GraphTopic]) -> list[str]:
    groups: dict[tuple[str, ...], list[str]] = {}
    every_node: dict[int, list[str]] = {}
    for item in graph:
        publishers = tuple(sorted(set(item.publishers)))
        if item.topic in EVERY_NODE_TOPICS:
            every_node.setdefault(len(publishers), []).append(item.topic)
        else:
            groups.setdefault(publishers, []).append(item.topic)
    ordered = sorted(
        groups.items(),
        key=lambda group: (
            not MACHINERY_NODES & set(group[0]),
            not group[0],
            group[0],
        ),
    )
    return [
        f"graph {' '.join(publishers) or '(no publisher)'}: {' '.join(sorted(topics))}"
        for publishers, topics in ordered
    ] + [
        f"graph {count} nodes: {' '.join(sorted(topics))}"
        for count, topics in sorted(every_node.items())
    ]


def water_fill(room: int, sizes: Sequence[int]) -> list[int]:
    shares = [0] * len(sizes)
    left = max(room, 0)
    order = sorted(range(len(sizes)), key=lambda index: sizes[index])
    for position, index in enumerate(order):
        shares[index] = min(sizes[index], left // (len(order) - position))
        left -= shares[index]
    return shares


def _safe(text: str) -> str:
    return redact_line(text) or UNRECORDED


def _run_lines(diagnostic: Diagnostic, run: RunOutcome | None) -> None:
    if run is None:
        return
    for phase, seconds in run.phases.items():
        diagnostic.line(f"phase {phase}: {seconds} s")
    match run:
        case Finished(recording=Recording(video=video, summary=summary) as kept):
            diagnostic.line(
                _safe(
                    f"recording: sim-recording.mp4 {len(video)} bytes, {summary}, "
                    f"stopped in {kept.stop_seconds} s"
                )
            )
        case Finished(recording=RecordingFailed(detail=detail)):
            diagnostic.line(_safe(f"recording: none: {detail}"))
        case _:
            pass


def _mission_lines(diagnostic: Diagnostic, scenario: ScenarioResult) -> None:
    mission = scenario.mission
    if mission is None:
        return
    setup = mission.setup
    waited = "" if setup.waited_on is None else f" on {setup.waited_on}"
    diagnostic.line(f"setup: {setup.status} {setup.elapsed_s} s{waited}")
    for step in mission.steps:
        parts = [f"step {step.requirement_id}: {step.status}"]
        if step.waited_on is not None:
            parts.append(step.waited_on)
        if step.elapsed_s is not None:
            parts.append(f"{step.elapsed_s} s")
        diagnostic.line(" ".join(parts))


def _service_line(item: ServiceParticipation) -> str:
    parts = [f"service {item.service_id}: {item.participation}"]
    if isinstance(item, (NonParticipatingService, UnevaluatedService)):
        parts.append(item.reason)
        if isinstance(item, NonParticipatingService) and item.exit_code is not None:
            parts.append(f"exit {item.exit_code}")
        if item.detail is not None:
            parts.append(item.detail)
    return " ".join(parts)


def _count_lines(
    diagnostic: Diagnostic, judgement: Judgement, scenario: ScenarioResult
) -> None:
    graphed = {item.topic: item.types for item in scenario.graph}
    for output in judgement.counting:
        if output.topic not in scenario.counts:
            continue
        line = (
            f"count {output.topic} {output.type}: {scenario.counts[output.topic]} "
            f"({output.service_id})"
        )
        types = graphed.get(output.topic, [])
        if types and output.type not in types:
            line += f" graph has {' '.join(types)}"
        diagnostic.line(_safe(line))
    for item in scenario.unloadable_types:
        diagnostic.line(_safe(f"unloadable {item.topic} {item.type}"))


def _graph_section(diagnostic: Diagnostic, scenario: ScenarioResult) -> None:
    missing, extra = topic_constant_drift(scenario.graph)
    drift = "; ".join(
        f"{name} {' '.join(sorted(topics))}"
        for name, topics in (("missing", missing), ("extra", extra))
        if topics
    )
    diagnostic.line(f"topic-constant: {drift or 'match'}")
    lines = [_safe(line) for line in graph_lines(scenario.graph)]
    used = 0
    for index, line in enumerate(lines):
        used += len(line.encode()) + 1
        if used > GRAPH_MAX_BYTES:
            diagnostic.line(f"graph: {len(lines) - index} more publisher sets cut")
            diagnostic.mark_cut()
            return
        diagnostic.line(line)


def _tails(
    diagnostic: Diagnostic, outcome: Outcome | Succeeded, result: Judgement | CvError
) -> None:
    room = diagnostic.room(outcome)
    tails = [
        (title, tail_lines(state.log_tail, max_bytes=room))
        for title, state in _tailed(result)
    ]
    shares = water_fill(room, [block_cost(title, tail.lines) for title, tail in tails])
    for (title, tail), share in zip(tails, shares, strict=True):
        if share >= block_cost(title, tail.lines):
            diagnostic.block(title, tail.lines, cut=tail.cut)
            continue
        lines_room = share - block_cost(title, ())
        if lines_room <= 0:
            diagnostic.mark_cut()
            continue
        kept = tail_lines(tail.lines, max_bytes=lines_room).lines
        diagnostic.block(title, kept, cut=True)


def _tailed(result: Judgement | CvError) -> list[tuple[str, ContainerState]]:
    if isinstance(result, CvError):
        snapshot = None if result.run is None else result.run.snapshot
        if snapshot is None:
            return []
        return [
            ("sim log tail", snapshot.sim),
            ("orchestrator log tail", snapshot.orchestrator),
        ]
    run = result.run
    if not isinstance(run, Finished):
        return []
    placements = run.snapshot.placements
    tailed = [
        (f"{item.service_id} log tail", state)
        for item in result.services
        if isinstance(item, NonParticipatingService)
        and isinstance(state := placements.get(item.service_id), ContainerState)
    ]
    if validation_verdict(result.services, result.mission) == "failed":
        tailed.append(("orchestrator log tail", run.snapshot.orchestrator))
    return tailed

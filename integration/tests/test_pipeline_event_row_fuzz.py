"""`build_row` never raises, whatever profile, attempts and reports it gets."""

from __future__ import annotations

import random
from dataclasses import replace
from functools import cache
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._pipeline_event_catalogue import (
    SLOT_TYPES,
    STEPS,
    template_slots,
)
from sdi_pipeline_integration._pipeline_event_rows import (
    ROW_SLOTS,
    EventLine,
    StageFinished,
    build_row,
)
from sdi_pipeline_integration._pipeline_events import PipelineEventRow, encode_event
from sdi_pipeline_integration._stage_registry import STAGE_ORDER
from tests._pipeline_event_scenarios import SCENARIOS
from tests._pipeline_event_world import (
    EMITTED_AT,
    IDENTIFIED,
    PLACEMENTS,
    PROFILES,
    profile_source,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sdi_pipeline_integration._pipeline_event_catalogue import (
        SlotName,
        SlotValue,
    )
    from sdi_pipeline_integration._pipeline_event_rows import LineHead
    from sdi_pipeline_integration._stage_registry import StageName
    from sdi_pipeline_integration._stage_runtime import (
        StageExecution,
        StageInputSource,
    )

SEEDS = range(12)
ROWS_PER_SEED = 40
IDENTIFIERS: dict[str, tuple[str, ...]] = {
    "service_id": tuple(item.service_id for item in PLACEMENTS),
    "host": ("waffle", "orin", "xycar"),
    "provider": ("luna", "qwen"),
}
HOSTILE_PROFILES = (
    b"",
    b"\xff\xfe\x00",
    b"hosts: [unclosed",
    b"{}",
    b"- a\n- b\n",
    b"schema_version: sdi.target-execution-profile/v1\nhosts: 3\n",
    b"&a [*a]",
    b"a: " + b"[" * 200 + b"]" * 200,
)


@cache
def _attempts() -> tuple[StageExecution, ...]:
    """Every distinct attempt the golden scenarios publish."""
    found: dict[int, StageExecution] = {}
    for scenario in SCENARIOS.values():
        for make in scenario.finished.values():
            found.setdefault(id(make), make())
    return tuple(found.values())


def _slots(names: frozenset[SlotName], rng: random.Random) -> dict[SlotName, SlotValue]:
    slots: dict[SlotName, SlotValue] = {}
    for name in names:
        slot = SLOT_TYPES[name]
        match slot.kind:
            case "integer":
                slots[name] = rng.randrange(0, 10_000)
            case "enum":
                slots[name] = rng.choice(slot.values)
            case "identifier":
                slots[name] = rng.choice(IDENTIFIERS[name])
    return slots


@cache
def _heads_by_seed(seed: int) -> tuple[EventLine, ...]:
    """One valid head for every step form, Stage and heartbeat the catalogue has."""
    rng = random.Random(seed)  # noqa: S311
    heads: list[EventLine] = []
    for step in STEPS:
        if step.code == "stage-finished":
            continue
        stages: list[StageName | None] = [
            stage for stage in (None, *STAGE_ORDER) if step.allows_stage(stage)
        ]
        templates = [(form, False) for form in step.forms]
        if step.heartbeat is not None:
            templates.append((step.heartbeat, True))
        for stage in stages:
            for template, heartbeat in templates:
                names = template_slots(template) - {"Stage"} - ROW_SLOTS
                heads.append(
                    EventLine(stage, step.code, _slots(names, rng), heartbeat=heartbeat)
                )
    return tuple(heads)


def _profile(rng: random.Random) -> StageInputSource:
    committed = sorted(path.stem for path in PROFILES.glob("*.yaml"))
    source = profile_source(rng.choice(committed))
    content = bytearray(source.content)
    match rng.randrange(5):
        case 0:
            return source
        case 1:
            return replace(
                source, content=bytes(content[: rng.randrange(len(content))])
            )
        case 2:
            for _ in range(rng.randrange(1, 8)):
                content[rng.randrange(len(content))] = rng.randrange(256)
            return replace(source, content=bytes(content))
        case 3:
            return replace(source, content=rng.choice(HOSTILE_PROFILES))
        case _:
            return replace(source, media_type=rng.choice(("text/markdown", "")))


def _damaged(execution: StageExecution, rng: random.Random) -> StageExecution:
    match rng.randrange(4):
        case 0:
            return replace(execution, files={})
        case 1:
            return replace(
                execution,
                files={
                    path: rng.randbytes(rng.randrange(64)) for path in execution.files
                },
            )
        case 2:
            return replace(
                execution,
                files={
                    path: content[: rng.randrange(len(content) + 1)]
                    for path, content in execution.files.items()
                },
            )
        case _:
            return execution


def _executions(rng: random.Random) -> list[StageExecution]:
    pool = _attempts()
    chosen = rng.sample(pool, rng.randrange(0, 7))
    if chosen and rng.random() < 0.3:
        chosen.append(rng.choice(chosen))
    return [_damaged(item, rng) for item in chosen]


def _head(rng: random.Random, heads: Sequence[EventLine]) -> LineHead:
    if rng.random() < 0.3:
        return StageFinished(_damaged(rng.choice(_attempts()), rng))
    return rng.choice(heads)


@pytest.mark.parametrize("seed", SEEDS)
def test_build_row_never_raises(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    heads = _heads_by_seed(seed)
    reports = [head for head in heads if not head.heartbeat]
    for _ in range(ROWS_PER_SEED):
        executions = _executions(rng)
        profile = _profile(rng)
        head = _head(rng, heads)
        live_items = rng.sample(reports, rng.randrange(0, 12))
        scope = rng.choice(("run", "single_stage"))
        row = build_row(
            IDENTIFIED,
            profile,
            executions,
            line=head,
            live_items=live_items,
            scope=scope,
            emitted_at=EMITTED_AT,
        )

        assert PipelineEventRow.model_validate(row.model_dump()) == row
        assert encode_event(row).endswith(b"\n")
        stages = [item.envelope.correlation.stage for item in executions]
        if len(set(stages)) == len(stages):
            shuffled = rng.sample(executions, len(executions))
            again = build_row(
                IDENTIFIED,
                profile,
                shuffled,
                line=head,
                live_items=live_items,
                scope=scope,
                emitted_at=EMITTED_AT,
            )
            assert again == row, "the order of executions changed the row"


def test_every_catalogue_head_is_a_valid_event_line() -> None:
    heads = _heads_by_seed(0)

    assert {head.step for head in heads} == {
        step.code for step in STEPS if step.code != "stage-finished"
    }
    assert any(head.heartbeat for head in heads)
    assert len(heads) > len(STEPS)

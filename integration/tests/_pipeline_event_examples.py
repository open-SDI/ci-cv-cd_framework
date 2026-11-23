"""The spec 4.8 example lines, built by `build_row` and written by `write_event`.

Regenerate the committed files from `integration/` with
`uv run --frozen python -m tests._pipeline_event_examples`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sdi_pipeline_integration._pipeline_event_rows import EventLine
from tests._pipeline_event_world import (
    EMITTED_AT,
    Placement,
    build,
    cd_deployed,
    composition_ok,
    cv_passed,
    images_built,
)
from tests._pipeline_events import GOLDEN_DIRECTORY, written
from tests._stage_attempts import finalized

if TYPE_CHECKING:
    from sdi_pipeline_integration._pipeline_events import PipelineEventRow

EXAMPLE_DIRECTORY: Final = GOLDEN_DIRECTORY / "examples"
PLACEMENTS: Final = (
    Placement("nav2-localization", "jazzy-debs", "waffle"),
    Placement("nav2-navigation", "jazzy-debs", "waffle"),
    Placement("turtlebot3-bringup", "jazzy-debs", "waffle"),
    Placement("vosk-speech", "jazzy-debs", "orin"),
    Placement("face-recog", "jazzy-source", "orin"),
)


def _scripted(step: str, index: int) -> EventLine:
    return EventLine("cv", "mission-step", {"mission_step": step, "i": index, "n": 8})


def _passed(step: str, index: int, seconds: int) -> EventLine:
    return EventLine(
        "cv",
        "mission-step",
        {
            "mission_step": step,
            "mission_status": "passed",
            "s": seconds,
            "i": index,
            "n": 8,
        },
    )


def mid_cv() -> PipelineEventRow:
    """Return spec 1's line: CV reports its fourth mission step while it runs."""
    reports = [
        EventLine("cv", "mission"),
        EventLine("cv", "mission-setup-done", {"setup_status": "passed", "s": 41}),
        _scripted("secure-book", 1),
        _passed("accept-spoken-request", 2, 37),
        _scripted("resolve-rendezvous", 3),
        _passed("navigate-to-rendezvous", 4, 212),
    ]
    return build(
        [composition_ok(PLACEMENTS), images_built(PLACEMENTS)],
        line=reports[-1],
        live_items=reports,
        emitted_at=EMITTED_AT + timedelta(minutes=23, seconds=7),
    )


def g1_final() -> PipelineEventRow:
    """Return the final line of a run in which every Stage succeeded."""
    held = [
        composition_ok(PLACEMENTS),
        images_built(PLACEMENTS),
        cv_passed(PLACEMENTS),
        cd_deployed(PLACEMENTS),
    ]
    return build(
        finalized(held, run_deadline_expired=False),
        line=EventLine(None, "run-finished"),
        emitted_at=EMITTED_AT + timedelta(minutes=41, seconds=52),
    )


EXAMPLES: Final = {"mid-cv": mid_cv, "g1-final": g1_final}


def main() -> None:
    """Write each example's line pair to its committed file."""
    EXAMPLE_DIRECTORY.mkdir(exist_ok=True)
    for name, example in EXAMPLES.items():
        (EXAMPLE_DIRECTORY / f"{name}.txt").write_bytes(written(example()))


if __name__ == "__main__":
    main()

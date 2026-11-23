"""The shared diagnostic publishes only screened text within its byte limit."""

from __future__ import annotations

from sdi_pipeline_integration._adapter_kit import Diagnostic, Outcome

RECORDED = Outcome("recorded", "succeeded", "succeeded", "domain", "Recorded.")
OUTCOME_LINE = b"outcome: sdi.kit.recorded\n"


def test_a_flagged_line_is_replaced_by_a_placeholder() -> None:
    diagnostic = Diagnostic("sdi.kit", 4096)
    diagnostic.line("clean line")
    diagnostic.line("pulled from https://registry.example/v2")

    assert diagnostic.render(RECORDED) == (
        OUTCOME_LINE + b"clean line\nunrecorded\n",
        False,
    )


def test_a_header_flagged_only_once_joined_keeps_the_outcome_line_alone() -> None:
    diagnostic = Diagnostic("sdi.kit", 4096)
    diagnostic.fact("token", "abc")
    diagnostic.block("log tail", ["ok"], cut=False)

    assert diagnostic.render(RECORDED) == (OUTCOME_LINE, True)


def test_a_flagged_header_over_budget_still_keeps_only_the_outcome_line() -> None:
    diagnostic = Diagnostic("sdi.kit", len(OUTCOME_LINE) + len(b"token: abc\n") + 2)
    diagnostic.fact("token", "abc")
    diagnostic.line("more")

    assert diagnostic.render(RECORDED) == (OUTCOME_LINE, True)


def test_a_header_over_budget_is_cut_after_its_last_whole_line() -> None:
    diagnostic = Diagnostic("sdi.kit", len(OUTCOME_LINE) + len(b"first\n") + 3)
    diagnostic.line("first")
    diagnostic.line("second")

    assert diagnostic.render(RECORDED) == (OUTCOME_LINE + b"first\n", True)


def test_an_outcome_line_over_budget_is_cut_to_the_budget() -> None:
    diagnostic = Diagnostic("sdi.kit", 10)

    assert diagnostic.render(RECORDED) == (OUTCOME_LINE[:10], True)


def test_a_block_sized_by_its_room_fills_the_budget_exactly() -> None:
    diagnostic = Diagnostic("sdi.kit", 100)
    room = diagnostic.block_room(RECORDED, "log tail", shares=1)
    diagnostic.block("log tail", ["x" * (room - 1)], cut=False)

    content, truncated = diagnostic.render(RECORDED)

    assert (len(content), truncated) == (100, False)
    assert content.endswith(b"x\n")

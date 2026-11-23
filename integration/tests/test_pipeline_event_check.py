"""The pipeline-event conformance checker against the golden files."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator

from sdi_pipeline_integration._cli import main
from sdi_pipeline_integration._pipeline_event_check import (
    Violation,
    allowed_final_statuses,
    check_events,
    load_golden,
)
from sdi_pipeline_integration._yaml_input import InputError
from tests._pipeline_event_scenarios import golden_console, golden_rows
from tests._pipeline_events import (
    GOLDEN_DIRECTORY,
    SCHEMA_PATH,
    console,
    golden,
    golden_names,
    row,
    with_row,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sdi_pipeline_integration._pipeline_event_catalogue import PipelineStatus
    from sdi_pipeline_integration._pipeline_event_check import StatusVector
    from sdi_pipeline_integration._pipeline_events import PipelineEventRow
    from sdi_pipeline_integration._stage_registry import StageName

SPEC_GOLDENS = {
    *(f"g{number}" for number in range(1, 22) if number != 9),
    "g2b",
    "g15b",
    "g9-no-node",
    "g9-killed-cv",
    "g9-race",
}
DEADLINE = "sdi.run.deadline-exceeded"


def _rules(violations: list[Violation]) -> set[str]:
    return {violation.rule for violation in violations}


def _index(rows: list[PipelineEventRow], stage: StageName | None, step: str) -> int:
    return next(
        index
        for index, event in enumerate(rows)
        if event.stage == stage and event.step == step
    )


def _vector(letters: str) -> StatusVector:
    names: dict[str, PipelineStatus] = {
        "P": "PENDING",
        "R": "RUNNING",
        "S": "SUCCEEDED",
        "F": "FAILED",
        "K": "SKIPPED",
        "N": "NOT_EVALUATED",
    }
    statuses: list[PipelineStatus] = [names[letter] for letter in letters.split()]
    first, second, third, fourth = statuses
    return (first, second, third, fourth)


def test_every_spec_golden_has_a_file() -> None:
    assert set(golden_names()) == SPEC_GOLDENS


def test_g14_keeps_g1s_lines() -> None:
    def lines(name: str) -> list[object]:
        return [(line.projection, line.assertions) for line in golden(name).lines]

    assert lines("g14") == lines("g1"), "G14 must stay G1's lines (spec 9.2 G14)"


@pytest.mark.parametrize("name", sorted(SPEC_GOLDENS))
def test_checker_accepts_every_golden_against_itself(name: str) -> None:
    assert check_events(golden_console(name), golden(name)) == []


@pytest.mark.parametrize("name", sorted(SPEC_GOLDENS))
def test_real_run_accepts_every_golden(name: str) -> None:
    assert check_events(golden_console(name), None) == []


@pytest.mark.parametrize("name", ["g1", "g15", "g20"])
def test_checker_accepts_goldens_without_shell_noise(name: str) -> None:
    assert check_events(golden_console(name, noise=False), golden(name)) == []


@pytest.mark.parametrize("name", sorted(SPEC_GOLDENS))
def test_golden_rows_validate_against_the_committed_schema(name: str) -> None:
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_bytes()))
    for line in golden_console(name).splitlines():
        if line.startswith(b"PRISM_EVENT "):
            validator.validate(json.loads(line.removeprefix(b"PRISM_EVENT ")))  # pyright: ignore[reportUnknownMemberType]


def test_checker_ignores_adapter_lines_and_dropped_reports_inside_a_stage() -> None:
    rows = golden_rows("g1")
    running = rows[_index(rows, "cv", "adapter-running")]
    mission = with_row(
        [running],
        0,
        step="mission",
        message="Mission setup (face, localization, navigation)",
    )[0]
    heartbeat = with_row([mission], 0, message="Mission still running, 3 min")[0]
    at = _index(rows, "cv", "checking-output")
    data = console(rows[:at]) + console([mission, heartbeat])
    data += b"sdi-integration: dropped 2 progress reports (step=2)\n"
    data += console(rows[at:])
    assert check_events(data, golden("g1")) == []


@pytest.mark.parametrize(
    ("message", "rules"),
    [
        ("Step secure-book: scripted (1/8)", set[str]()),
        ("Step secure-book: passed (1/8)", {"message"}),
    ],
)
def test_scripted_mission_step_says_scripted(message: str, rules: set[str]) -> None:
    rows = golden_rows("g1")
    running = rows[_index(rows, "cv", "adapter-running")]
    step = with_row([running], 0, step="mission-step", message=message)[0]
    at = _index(rows, "cv", "checking-output")
    data = console([*rows[:at], step, *rows[at:]])
    assert _rules(check_events(data, golden("g1"))) == rules


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("RUN run-accepted [P P P P] -\n    cv_staus != null\n", "cv_staus"),
        ("RUN run-accepted [P P P P P] -\n", "projection"),
        ("    blueprint_ref != null\nRUN run-accepted [P P P P] -\n", "before"),
        ("cv mission [S S R P] -\n", "runtime"),
        ("RUN run-accepted [P P P P] -\n    images_built ~ 3\n", "assertion"),
    ],
)
def test_golden_parser_rejects_malformed_goldens(
    text: str, match: str, tmp_path: Path
) -> None:
    path = tmp_path / "bad.txt"
    path.write_text(text)
    with pytest.raises(InputError, match=match):
        load_golden(path)


def test_rejects_a_line_out_of_order() -> None:
    rows = golden_rows("g1")
    first = _index(rows, "composition", "adapter-running")
    rows[first], rows[first + 1] = rows[first + 1], rows[first]
    violations = check_events(console(rows), golden("g1"))
    assert {"golden", "path"} <= _rules(violations), violations


def test_rejects_a_changed_status() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "image_build", "stage-finished")
    mutated = with_row(rows, at, image_build_status="FAILED", ci_status="FAILED")
    assert "golden" in _rules(check_events(console(mutated), golden("g1")))


def test_rejects_a_missing_sdi_partner() -> None:
    lines = golden_console("g1").splitlines(keepends=True)
    sdi = next(i for i, line in enumerate(lines) if line.startswith(b"[sdi] "))
    violations = check_events(b"".join(lines[:sdi] + lines[sdi + 1 :]), golden("g1"))
    assert _rules(violations) == {"pairing"}, violations
    assert violations[0].line == sdi + 1


def test_rejects_a_shell_line_between_partners() -> None:
    lines = golden_console("g1", noise=False).splitlines(keepends=True)
    lines.insert(1, b"+ echo between\n")
    violations = check_events(b"".join(lines), None)
    assert _rules(violations) == {"pairing"}, violations


def test_rejects_a_timestamper_prefix() -> None:
    data = console(golden_rows("g1"), decorate=lambda line: b"[09:00:00] " + line)
    assert "decorated" in _rules(check_events(data, None))


def test_rejects_two_final_lines() -> None:
    rows = golden_rows("g1")
    violations = check_events(console([*rows, rows[-1]]), None)
    assert "final" in _rules(violations), violations


def test_rejects_a_succeeded_that_changes_on_a_non_final_line() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cv", "publishing")
    mutated = with_row(rows, at, composition_status="FAILED", ci_status="FAILED")
    assert "succeeded-stable" in _rules(check_events(console(mutated), None))


def test_rejects_succeeded_before_its_own_stage_finished() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cv", "publishing")
    mutated = with_row(rows, at, cv_status="SUCCEEDED")
    assert "succeeded-origin" in _rules(check_events(console(mutated), None))


def test_reports_a_stage_whose_success_came_early_once() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "image_build", "stage-finished")
    mutated = with_row(rows, at, image_build_status="FAILED", ci_status="FAILED")
    violations = check_events(console(mutated), None)
    origins = [v for v in violations if v.rule == "succeeded-origin"]
    assert len(origins) == 1, violations


def test_rejects_a_line_naming_a_finished_stage() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "composition", "stage-finished")
    late = with_row(rows, at, step="publishing", message="Recording Composition result")
    mutated = [*rows[: at + 1], late[at], *rows[at + 1 :]]
    assert "closed-stage" in _rules(check_events(console(mutated), None))


def test_rejects_a_changed_identity() -> None:
    rows = with_row(golden_rows("g1"), 3, testcase_id="TC-04")
    assert "identity" in _rules(check_events(console(rows), None))


def test_rejects_an_unknown_step_and_a_step_on_the_wrong_stage() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cd", "adapter-running")
    unknown = with_row(rows, at, step="warming-up")
    assert "catalogue" in _rules(check_events(console(unknown), None))
    wrong = with_row(rows, at, step="pull", message="Pulled navigation (1/2)")
    assert "catalogue" in _rules(check_events(console(wrong), None))


def test_rejects_a_free_text_message() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cv", "stage-started")
    mutated = with_row(rows, at, message="CV started, looking good")
    assert _rules(check_events(console(mutated), None)) == {"message"}


def test_rejects_a_ci_status_that_breaks_section_5_2() -> None:
    rows = golden_rows("g15")
    at = _index(rows, "composition", "stage-finished")
    mutated = with_row(rows, at, ci_status="NOT_EVALUATED")
    assert "ci-status" in _rules(check_events(console(mutated), None))


def test_rejects_a_status_column_that_differs_from_the_row() -> None:
    lines = golden_console("g1", noise=False).splitlines(keepends=True)
    at = next(i for i, line in enumerate(lines) if b"CV > stage-started  " in line)
    lines[at] = lines[at].replace(b"  RUNNING  ", b"  PENDING  ")
    assert _rules(check_events(b"".join(lines), None)) == {"status-column"}


def test_rejects_a_prism_line_that_is_not_the_renderers() -> None:
    lines = golden_console("g1", noise=False).splitlines(keepends=True)
    payload = json.loads(lines[1].removeprefix(b"PRISM_EVENT "))
    lines[1] = b"PRISM_EVENT " + json.dumps(payload).encode() + b"\n"
    assert _rules(check_events(b"".join(lines), None)) == {"prism-line"}


def test_rejects_an_oversized_sdi_line() -> None:
    lines = golden_console("g1", noise=False).splitlines(keepends=True)
    lines[0] = lines[0].rstrip(b"\n") + b"x" * 1024 + b"\n"
    violations = check_events(b"".join(lines), None)
    assert [(violation.rule, violation.line) for violation in violations] == [
        ("size", 1)
    ], violations


def test_accepts_a_prism_line_of_any_length() -> None:
    rows = golden_rows("g1")
    images = 200
    rows = with_row(
        rows,
        len(rows) - 1,
        image_ref=[f"registry:5000/sdi/service-{i:03d}:tag" for i in range(images)],
        image_digest=["sha256:" + "0" * 64 for _ in range(images)],
    )
    data = console(rows)
    assert max(map(len, data.splitlines())) > 16 * 1024
    assert check_events(data, None) == []


@pytest.mark.parametrize("raw", [b"\x7f", b"\x1b", b"##["])
def test_rejects_forbidden_characters(raw: bytes) -> None:
    lines = golden_console("g1", noise=False).splitlines(keepends=True)
    lines[0] = lines[0].replace(b"Run accepted", b"Run " + raw + b"accepted")
    assert "characters" in _rules(check_events(b"".join(lines), None))


def test_rejects_a_line_that_does_not_parse() -> None:
    lines = golden_console("g1", noise=False).splitlines(keepends=True)
    lines[1] = b"PRISM_EVENT {not json}\n"
    assert _rules(check_events(b"".join(lines), None)) == {"parse"}


def test_rejects_a_failed_extra_assertion() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "image_build", "stage-finished")
    mutated = with_row(rows, at, platform_status="MISMATCH")
    violations = check_events(console(mutated), golden("g1"))
    assert _rules(violations) == {"extra"}, violations
    assert 'platform_status == "MATCH"' in violations[0].detail


def test_rejects_a_console_shorter_than_its_golden() -> None:
    rows = golden_rows("g1")
    violations = check_events(console(rows[:-1]), golden("g1"))
    assert "golden" in _rules(violations)


def test_real_run_rejects_a_stage_off_path() -> None:
    rows = golden_rows("g1")
    del rows[_index(rows, "cv", "adapter-running")]
    assert "path" in _rules(check_events(console(rows), None))


def test_real_run_rejects_a_killed_prefix_before_the_last_stage() -> None:
    rows = golden_rows("g1")
    del rows[_index(rows, "image_build", "stage-finished")]
    assert "path" in _rules(check_events(console(rows), None))


def test_real_run_rejects_stages_out_of_order() -> None:
    rows = golden_rows("g1")
    image_build = [event for event in rows if event.stage == "image_build"]
    others = [event for event in rows if event.stage != "image_build"]
    at = _index(others, "cv", "stage-finished") + 1
    violations = check_events(console([*others[:at], *image_build, *others[at:]]), None)
    assert "path" in _rules(violations)


def test_real_run_rejects_adapter_lines_outside_the_adapter_window() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cv", "checking-output")
    late = with_row(
        rows,
        at,
        step="mission",
        message="Mission setup (face, localization, navigation)",
    )[at]
    violations = check_events(console([*rows[: at + 1], late, *rows[at + 1 :]]), None)
    assert _rules(violations) == {"path"}, violations


def test_real_run_rejects_a_dropped_reports_line_after_publishing() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cv", "publishing") + 1
    dropped = b"sdi-integration: dropped 1 progress reports (step=1)\n"
    violations = check_events(console(rows[:at]) + dropped + console(rows[at:]), None)
    assert _rules(violations) == {"path"}, violations


def test_real_run_rejects_final_statuses_that_differ_without_a_run_deadline() -> None:
    rows = golden_rows("g2")
    mutated = with_row(rows, len(rows) - 1, cd_status="FAILED")
    assert "final-statuses" in _rules(check_events(console(mutated), None))


def test_real_run_rejects_a_stage_not_running_on_its_own_lines() -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cv", "adapter-running")
    mutated = with_row(rows, at, cv_status="PENDING")
    assert _rules(check_events(console(mutated), None)) == {"stage-status"}


@pytest.mark.parametrize("status", ["RUNNING", "PENDING"])
def test_real_run_rejects_a_stage_finished_that_is_not_terminal(status: str) -> None:
    rows = golden_rows("g1")
    at = _index(rows, "cd", "stage-finished")
    mutated = with_row(rows, at, cd_status=status)
    assert "stage-status" in _rules(check_events(console(mutated), None))


def test_real_run_accepts_a_run_deadline_after_cv_not_evaluated() -> None:
    rows = golden_rows("g21")
    at = _index(rows, "cv", "stage-finished")
    final = with_row(
        rows,
        len(rows) - 1,
        cv_status="FAILED",
        cd_status="SKIPPED",
        failure_reason=DEADLINE,
        message="Final: CI SUCCEEDED, CV FAILED, CD SKIPPED",
    )[-1]
    assert check_events(console([*rows[: at + 1], final]), None) == []


@pytest.mark.parametrize(
    ("last_finished", "stage", "final", "reason", "allowed"),
    [
        ("S S S P", "cv", "S S S F", DEADLINE, True),
        ("S S P P", "image_build", "S S F K", DEADLINE, True),
        ("S S S P", "cv", "S S F K", DEADLINE, True),
        ("S S N P", "cv", "S S F K", DEADLINE, True),
        ("S S N P", "cv", "S S F K", "sdi.cv.not-evaluated", False),
        ("S S N P", "cv", "S F K K", DEADLINE, False),
        ("P P P P", None, "F K K K", DEADLINE, True),
        ("S S F K", "cv", "S S F K", "sdi.cv.failed", True),
        ("S S S P", "cv", "S S S F", "sdi.cd.failed", False),
        ("S S S P", "cv", "S F K K", DEADLINE, False),
        ("S S P P", "image_build", "S S S F", DEADLINE, False),
        ("S S S S", "cd", "S S F K", DEADLINE, False),
    ],
)
def test_allowed_final_statuses_cover_the_run_deadline_cases(
    last_finished: str,
    stage: StageName | None,
    final: str,
    reason: str,
    *,
    allowed: bool,
) -> None:
    statuses = allowed_final_statuses(_vector(last_finished), reason, stage)
    assert (_vector(final) in statuses) is allowed


def test_empty_console_conforms() -> None:
    assert check_events(b"", None) == []
    assert check_events(b"+ sh -x\nStarted by user\n", None) == []


def test_violation_names_its_file_line_and_rule() -> None:
    violation = Violation("pairing", "no [sdi] partner", 12)
    assert (
        violation.render("consoleText") == "consoleText:12: [pairing] no [sdi] partner"
    )


def test_check_events_cli_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    goldens = ["--goldens", str(GOLDEN_DIRECTORY)]
    conforming = tmp_path / "g1.txt"
    conforming.write_bytes(golden_console("g1"))
    assert main(["check-events", str(conforming), "--golden", "g1", *goldens]) == 0
    real = tmp_path / "g8.txt"
    real.write_bytes(golden_console("g8"))
    assert main(["check-events", str(real), "--real-run"]) == 0
    capsys.readouterr()

    mutated = tmp_path / "mutated.txt"
    mutated.write_bytes(golden_console("g1") + console([row()]))
    assert main(["check-events", str(mutated), "--golden", "g1", *goldens]) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"{mutated}:"), out
    assert "[golden]" in out

    missing = tmp_path / "missing.txt"
    assert main(["check-events", str(missing), "--real-run"]) == 2
    assert main(["check-events", str(conforming), "--golden", "g99", *goldens]) == 2
    assert main(["check-events", str(conforming), "--golden", "../g1", *goldens]) == 2
    assert "sdi-integration: " in capsys.readouterr().err

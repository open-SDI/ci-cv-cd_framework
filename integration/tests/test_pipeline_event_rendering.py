"""The pipeline-event renderers and writer."""

from __future__ import annotations

import errno
import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._pipeline_events import (
    PipelineEventRow,
    derive_ci_status,
    encode_event,
    format_emitted_at,
    render_message,
    render_prism_line,
    render_sdi_line,
    write_event,
)
from tests._pipeline_events import row

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

    from sdi_pipeline_integration._pipeline_event_catalogue import (
        PipelineStatus,
        SlotName,
    )
    from sdi_pipeline_integration._pipeline_events import EventScope

HOSTILE = "a##[b\x7fc\x1bd\te\xe9f\U0001f600g\\h"
CV_RUNNING: dict[str, JsonValue] = {
    "stage": "cv",
    "step": "stage-started",
    "message": "CV started",
    "composition_status": "SUCCEEDED",
    "image_build_status": "SUCCEEDED",
    "ci_status": "SUCCEEDED",
    "cv_status": "RUNNING",
}


def _payload(line: str) -> dict[str, JsonValue]:
    prefix, _, payload = line.partition(" ")
    assert prefix == "PRISM_EVENT"
    return json.loads(payload)


def _printable(data: bytes) -> bool:
    return all(0x20 <= byte <= 0x7E for byte in data)


def test_prism_line_is_compact_ascii_json_in_field_order() -> None:
    line = render_prism_line(row(**CV_RUNNING))
    payload = line.removeprefix("PRISM_EVENT ")
    assert line.startswith("PRISM_EVENT {")
    assert list(_payload(line)) == list(PipelineEventRow.model_fields)
    assert payload == json.dumps(
        _payload(line), separators=(",", ":"), ensure_ascii=True
    ), "the PRISM_EVENT JSON is not compact"


def test_prism_line_keeps_every_key_when_values_are_null() -> None:
    payload = _payload(render_prism_line(row()))
    assert set(payload) == set(PipelineEventRow.model_fields)
    assert payload["image_ref"] is None
    assert payload["image_archive"] is None


def test_both_lines_escape_bracket_after_hashes_and_del() -> None:
    data = encode_event(row(**{**CV_RUNNING, "message": "see ##[x and \x7f"}))
    sdi_line, prism_line, end = data.split(b"\n")
    assert end == b"", "the pair does not end with one LF"
    for line in (sdi_line, prism_line):
        assert b"##\\u005b" in line, line
        assert b"##[" not in line, line
        assert b"\\u007f" in line, line
        assert _printable(line), line
    assert _payload(prism_line.decode())["message"] == "see ##[x and \x7f"


def test_prism_line_escapes_controls_and_non_ascii() -> None:
    line = render_prism_line(row(**{**CV_RUNNING, "message": HOSTILE}))
    assert _printable(line.encode())
    for escape in ("\\u001b", "\\t", "\\u00e9", "\\ud83d\\ude00", "\\\\h"):
        assert escape in line, escape
    assert _payload(line)["message"] == HOSTILE


def test_sdi_line_stage_level_grammar() -> None:
    publishing = row(
        stage="image_build",
        step="publishing",
        message="Recording Image build result",
        composition_status="SUCCEEDED",
        image_build_status="RUNNING",
        ci_status="RUNNING",
    )
    assert render_sdi_line(publishing) == (
        "[sdi] C-05 IMAGE-BUILD > publishing  RUNNING  Recording Image build result"
    )


def test_sdi_line_run_level_has_no_status_column() -> None:
    assert render_sdi_line(row()) == (
        "[sdi] C-05 RUN > run-accepted  Run accepted for C-05 on waffle-jetson-arm64"
    )


def test_sdi_line_status_column_is_the_stages_own_status() -> None:
    line = render_sdi_line(row(**CV_RUNNING))
    assert line == "[sdi] C-05 CV > stage-started  RUNNING  CV started"


def test_sdi_line_escapes_every_byte_outside_printable_ascii() -> None:
    line = render_sdi_line(row(**{**CV_RUNNING, "message": HOSTILE}))
    assert line == (
        "[sdi] C-05 CV > stage-started  RUNNING  "
        "a##\\u005bb\\u007fc\\u001bd\\u0009e\\u00e9f\\ud83d\\ude00g\\\\h"
    )
    assert _printable(line.encode())


def test_render_message_removes_line_breaks_from_slot_values() -> None:
    message = render_message(
        "image-built",
        stage="image_build",
        slots={
            "service_id": (
                "na\nvi\r\x0bga\x0c\x85ti\N{LINE SEPARATOR}o\N{PARAGRAPH SEPARATOR}n"
            ),
            "i": 1,
            "n": 3,
        },
    )
    assert message == "navigation built (1/3)"


def test_render_message_fills_stage_from_its_event_label() -> None:
    message = render_message("adapter-running", stage="cv", slots={"limit_min": 12})
    assert message == "CV work started (limit 12 min)"


def test_render_message_selects_the_form_by_slot_set() -> None:
    assert (
        render_message(
            "stage-finished",
            stage="cv",
            slots={"status": "FAILED", "reason": "sdi.cv.failed"},
        )
        == "CV finished: FAILED (sdi.cv.failed)"
    )
    assert (
        render_message("stage-finished", stage="cv", slots={"status": "SUCCEEDED"})
        == "CV finished: SUCCEEDED"
    )
    assert (
        render_message(
            "mission-step",
            stage="cv",
            slots={
                "mission_step": "navigate-to-rendezvous",
                "mission_status": "passed",
                "s": 212,
                "i": 4,
                "n": 8,
            },
        )
        == "Step navigate-to-rendezvous: passed after 212 s (4/8)"
    )
    assert (
        render_message(
            "mission-step",
            stage="cv",
            slots={"mission_step": "secure-book", "i": 1, "n": 8},
        )
        == "Step secure-book: scripted (1/8)"
    )


def test_render_message_scripted_step_has_no_status_slot() -> None:
    with pytest.raises(ValueError, match="mission-step"):
        render_message(
            "mission-step",
            stage="cv",
            slots={
                "mission_step": "secure-book",
                "mission_status": "passed",
                "i": 1,
                "n": 8,
            },
        )


def test_render_message_fills_run_level_templates() -> None:
    assert (
        render_message(
            "run-finished",
            stage=None,
            slots={
                "ci_status": "SUCCEEDED",
                "cv_status": "FAILED",
                "cd_status": "SKIPPED",
            },
        )
        == "Final: CI SUCCEEDED, CV FAILED, CD SKIPPED"
    )


@pytest.mark.parametrize(
    "slots",
    [{}, {"limit_min": 3, "n": 1}],
    ids=["missing", "extra"],
)
def test_render_message_rejects_slots_matching_no_form(
    slots: dict[SlotName, int],
) -> None:
    with pytest.raises(ValueError, match="adapter-running"):
        render_message("adapter-running", stage="cv", slots=slots)


def test_render_message_rejects_a_stage_the_step_does_not_allow() -> None:
    with pytest.raises(ValueError, match="run-accepted"):
        render_message(
            "run-accepted",
            stage="cv",
            slots={"composition_id": "C-05", "profile_id": "waffle"},
        )
    with pytest.raises(ValueError, match="pull"):
        render_message("pull", stage="cd", slots={"service_id": "a", "i": 1, "n": 1})


def test_over_long_message_leaves_optional_slots_out_and_logs(
    capfd: pytest.CaptureFixture[str],
) -> None:
    message = render_message(
        "image-built",
        stage="image_build",
        slots={"service_id": "a" * 2000, "i": 1, "n": 3},
    )
    assert message == "- built (1/3)"
    errors = capfd.readouterr().err.splitlines()
    assert len(errors) == 1
    assert errors[0].startswith("sdi-integration: internal error: "), errors


class _Recorder:
    def __init__(self, fd: int, *, chunk: int | None = None) -> None:
        self.fd = fd
        self.chunk = chunk
        self.writes: list[bytes] = []
        self._write: Callable[[int, bytes], int] = os.write

    def __call__(self, fd: int, data: bytes) -> int:
        if fd != self.fd:
            return self._write(fd, data)
        written = bytes(data[: self.chunk] if self.chunk else data)
        self.writes.append(written)
        return len(written)


def test_write_event_writes_the_pair_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder(fd=2)
    monkeypatch.setattr(os, "write", recorder)
    started = row(**CV_RUNNING)
    write_event(started)
    monkeypatch.undo()
    assert recorder.writes == [encode_event(started)]


def test_write_event_finishes_a_partial_write(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(fd=2, chunk=100)
    monkeypatch.setattr(os, "write", recorder)
    started = row(**CV_RUNNING)
    write_event(started)
    monkeypatch.undo()
    assert b"".join(recorder.writes) == encode_event(started)
    assert len(recorder.writes) > 1


def test_write_event_ignores_a_closed_descriptor() -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)
    os.close(write_end)
    assert write_event(row(), fd=write_end) is None


def test_write_event_ignores_a_broken_pipe() -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        assert write_event(row(), fd=write_end) is None
    finally:
        os.close(write_end)


@pytest.mark.parametrize("error", [errno.EAGAIN, errno.EPIPE, errno.EBADF])
def test_write_event_ignores_write_errors(
    error: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_fd: int, _data: bytes) -> int:
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(os, "write", failing)
    result = write_event(row(), fd=99)
    monkeypatch.undo()
    assert result is None


def test_write_event_flushes_text_stderr_first(
    capfd: pytest.CaptureFixture[str],
) -> None:
    sys.stderr.write("sdi-integration: dropped 2 progress reports (step=2)\n")
    write_event(row())
    err = capfd.readouterr().err
    assert err.startswith("sdi-integration: dropped 2 progress reports"), err
    assert err.splitlines()[1].startswith("[sdi] C-05 RUN > run-accepted")


def test_write_event_ignores_a_missing_sys_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_end, write_end = os.pipe()
    monkeypatch.setattr(sys, "stderr", None)
    with os.fdopen(read_end, "rb") as pipe, os.fdopen(write_end, "wb") as sink:
        assert write_event(row(), fd=sink.fileno()) is None
        sink.close()
        assert pipe.read().startswith(b"[sdi] C-05 RUN > run-accepted")


@pytest.mark.parametrize(
    ("composition", "image_build", "scope", "expected"),
    [
        ("PENDING", "PENDING", "run", "PENDING"),
        ("PENDING", "PENDING", "single_stage", "PENDING"),
        ("RUNNING", "PENDING", "run", "RUNNING"),
        ("SUCCEEDED", "RUNNING", "run", "RUNNING"),
        ("PENDING", "RUNNING", "single_stage", "RUNNING"),
        ("SUCCEEDED", "SUCCEEDED", "run", "SUCCEEDED"),
        ("SUCCEEDED", "FAILED", "run", "FAILED"),
        ("SKIPPED", "FAILED", "run", "FAILED"),
        ("NOT_EVALUATED", "SKIPPED", "run", "SKIPPED"),
        ("SUCCEEDED", "SKIPPED", "run", "SKIPPED"),
        ("NOT_EVALUATED", "NOT_EVALUATED", "run", "NOT_EVALUATED"),
        ("NOT_EVALUATED", "SUCCEEDED", "run", "NOT_EVALUATED"),
        ("SUCCEEDED", "PENDING", "run", "RUNNING"),
        ("NOT_EVALUATED", "PENDING", "run", "RUNNING"),
        ("FAILED", "PENDING", "run", "RUNNING"),
        ("PENDING", "SUCCEEDED", "single_stage", "SUCCEEDED"),
        ("PENDING", "FAILED", "single_stage", "FAILED"),
        ("SUCCEEDED", "PENDING", "single_stage", "SUCCEEDED"),
    ],
)
def test_derive_ci_status_follows_section_5_2(
    composition: PipelineStatus,
    image_build: PipelineStatus,
    scope: EventScope,
    expected: PipelineStatus,
) -> None:
    assert derive_ci_status(composition, image_build, scope=scope) == expected


def test_format_emitted_at_is_utc_milliseconds_z() -> None:
    seoul = timezone(timedelta(hours=9))
    moment = datetime(2026, 10, 28, 18, 0, 1, 234_567, tzinfo=seoul)
    assert format_emitted_at(moment) == "2026-10-28T09:00:01.234Z"
    assert format_emitted_at(datetime(2026, 1, 2, tzinfo=UTC)) == (
        "2026-01-02T00:00:00.000Z"
    )
    with pytest.raises(ValueError, match="naive"):
        format_emitted_at(datetime(2026, 1, 2, tzinfo=UTC).replace(tzinfo=None))

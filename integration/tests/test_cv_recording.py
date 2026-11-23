"""CV attaches its overview recording, and a failed recording changes no verdict."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration import _cv_adapter
from sdi_pipeline_integration._adapter_kit import serve
from sdi_pipeline_integration._cv_adapter import validate
from sdi_pipeline_integration._cv_run import Recording, RecordingFailed
from sdi_pipeline_integration._stage_registry import STAGES
from tests._cv_builders import STRING, amd64_digest, finished
from tests._cv_stage import CvRun, CvStage, description, refusing_engine_url

if TYPE_CHECKING:
    from pathlib import Path

    from sdi_pipeline_integration._cv_run import RunOutcome, RunRequest

VIDEO = b"\x00\x00\x00\x18ftypmp42 not a real video"
SUMMARY = "subscribed True frames 3 skipped 0 ffmpeg exit 0"
(GRANT,) = STAGES["cv"].grant.attachments


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording: Recording | RecordingFailed | None,
) -> tuple[CvRun, list[RunRequest]]:
    requests: list[RunRequest] = []

    def run_composition(request: RunRequest, engine_url: str) -> RunOutcome:
        del engine_url
        requests.append(request)
        outcome = finished()(request.placements, request.counting)
        return replace(outcome, recording=recording)

    monkeypatch.setattr(_cv_adapter, "run_composition", run_composition)
    stage = CvStage(tmp_path)
    stage.write(
        {"vosk-speech": description("vosk-speech", {"/speech/text": STRING})},
        [
            {
                "service_id": "vosk-speech",
                "status": "built",
                "repository": "sdi/vosk-speech",
                "digest": amd64_digest("vosk-speech"),
                "platforms": [
                    {"architecture": "amd64", "digest": amd64_digest("vosk-speech")}
                ],
            }
        ],
    )
    run = stage.run(serve("cv", partial(validate, engine_url=refusing_engine_url())))
    return run, requests


def test_cv_asks_for_a_recording_up_to_its_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, requests = _run(tmp_path, monkeypatch, None)

    assert [item.recording_max_bytes for item in requests] == [GRANT.max_bytes]


def test_cv_attaches_the_recording_beside_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = _run(tmp_path, monkeypatch, Recording(VIDEO, SUMMARY, 2.5))

    assert run.response.execution_conclusion == "succeeded"
    assert run.response.domain_outcome == "succeeded"
    assert run.response.produced_outputs == ["validation_evidence", GRANT.name]
    assert (tmp_path / "candidate/outputs" / GRANT.file_name).read_bytes() == VIDEO
    assert (
        f"recording: sim-recording.mp4 {len(VIDEO)} bytes, {SUMMARY}, stopped in 2.5 s"
        in run.diagnostic.splitlines()
    )


@pytest.mark.parametrize(
    "recording",
    [RecordingFailed("the recorder stop exited 1: recorder never started"), None],
    ids=["failed", "absent"],
)
def test_cv_without_a_recording_reaches_the_same_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording: RecordingFailed | None,
) -> None:
    recorded, _ = _run(
        tmp_path / "recorded", monkeypatch, Recording(VIDEO, SUMMARY, 2.5)
    )

    run, _ = _run(tmp_path / "unrecorded", monkeypatch, recording)

    assert run.response.produced_outputs == ["validation_evidence"]
    assert not (tmp_path / "unrecorded/candidate/outputs" / GRANT.file_name).exists()
    assert run.evidence == recorded.evidence
    assert (run.response.execution_conclusion, run.response.domain_outcome) == (
        recorded.response.execution_conclusion,
        recorded.response.domain_outcome,
    )
    lines = run.diagnostic.splitlines()
    if recording is None:
        assert not [line for line in lines if line.startswith("recording:")]
    else:
        assert f"recording: none: {recording.detail}" in lines

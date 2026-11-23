"""Result files recorded from real cv_engine runs, read-only under a MANIFEST."""

from __future__ import annotations

import hashlib

from tests._cv_recordings import RECORDED, manifest, recorded

PASS = (
    "scripted",
    "passed",
    "scripted",
    "passed",
    "passed",
    "scripted",
    "passed",
    "passed",
)
NO_FACE = (
    "scripted",
    "passed",
    "scripted",
    "passed",
    "timed_out",
    *("not_reached",) * 3,
)


def test_cv_recordings_match_their_manifest() -> None:
    digests = manifest()

    assert digests.keys() == {path.name for path in RECORDED.glob("*.json")}
    for name, digest in digests.items():
        assert hashlib.sha256((RECORDED / name).read_bytes()).hexdigest() == digest


def _steps(name: str) -> tuple[str, ...]:
    result = recorded(name)
    assert result.mission is not None
    assert all(count >= 0 for count in result.counts.values())
    return tuple(item.status for item in result.mission.steps)


def test_cv_recorded_passes_complete_the_mission() -> None:
    assert _steps("waffle-pass") == PASS
    assert _steps("burger-pass") == PASS


def test_cv_recorded_no_face_run_times_out_at_the_face_step() -> None:
    assert _steps("no-face") == NO_FACE

"""Checks that build-log text reaches durable output only once redacted."""

from __future__ import annotations

from pathlib import Path

from sdi_pipeline_integration._domain_contracts import FailedImageRecord
from sdi_pipeline_integration._image_build_redaction import (
    LogTail,
    failure_detail,
    log_tail,
    redact_line,
)
from sdi_pipeline_integration._stage_contracts import contains_sensitive_material

# The shape of a cold arm64 nav2 build whose apt fetch failed, as BuildKit's
# plain progress prints it.
APT_FAILURE_LOG = Path(__file__).parent / "data/apt-failure.txt"


def test_every_kept_log_line_passes_the_runtime_check() -> None:
    lines = log_tail(APT_FAILURE_LOG, max_bytes=16384).lines

    assert lines
    assert not any(contains_sensitive_material(line) for line in lines)
    assert not any(ord(char) < 0x20 for line in lines for char in line)


def test_addresses_and_machine_paths_are_masked_not_dropped() -> None:
    line = redact_line(
        "#5 1.204   Could not connect to snapshots.ros.org:80 (10.0.3.7), "
        "connection timed out reading /var/lib/dpkg/status"
    )

    assert line == (
        "#5 1.204   Could not connect to snapshots.ros.org:80 (<address>), "
        "connection timed out reading <path>"
    )


def test_urls_and_private_hostnames_are_masked() -> None:
    assert redact_line("#5 0.412 Get:1 http://snapshots.ros.org/jazzy noble") == (
        "#5 0.412 Get:1 <url> noble"
    )
    assert redact_line("proxy at buildkitd.internal refused") == (
        "proxy at <host> refused"
    )


def test_a_line_carrying_a_credential_form_is_dropped() -> None:
    assert redact_line("#5 1.314 retrying with token=abc123") is None


def test_ansi_and_control_characters_are_removed() -> None:
    assert redact_line("\x1b[33mwarning\x1b[0m\tnext\x07") == "warning next"


def test_the_detail_keeps_the_exit_code_of_a_long_last_error_line() -> None:
    detail = failure_detail(APT_FAILURE_LOG, fallback="the build failed")

    assert len(detail) <= 256
    assert detail.startswith("error: failed to solve: process ")
    assert detail.endswith("did not complete successfully: exit code: 100")
    assert not contains_sensitive_material(detail)
    FailedImageRecord.model_validate(
        {
            "service_id": "nav2-navigation",
            "status": "failed",
            "reason": "build-failed",
            "detail": detail,
        },
        strict=True,
    )


def test_the_detail_falls_back_when_no_line_survives(tmp_path: Path) -> None:
    path = tmp_path / "build.log"
    path.write_bytes(b"\n  \nretrying with token=abc123\n")

    assert failure_detail(path, fallback="the build failed") == "the build failed"


def test_the_detail_falls_back_when_the_log_is_missing(tmp_path: Path) -> None:
    assert (
        failure_detail(tmp_path / "absent.log", fallback="the build failed")
        == "the build failed"
    )


def test_the_tail_keeps_the_last_lines_within_its_byte_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "build.log"
    path.write_bytes(b"".join(f"line {index}\n".encode() for index in range(10000)))

    tail = log_tail(path, max_bytes=40)

    assert tail.lines == ["line 9996", "line 9997", "line 9998", "line 9999"]
    assert tail.cut


def test_a_tail_that_holds_the_whole_log_is_not_cut(tmp_path: Path) -> None:
    path = tmp_path / "build.log"
    path.write_bytes(b"first\nsecond\n")

    assert log_tail(path, max_bytes=40) == LogTail(["first", "second"], cut=False)


def test_the_pipeline_registry_name_is_not_sensitive() -> None:
    name = "registry:5000/sdi/nav2-navigation:jazzy-debs"

    assert not contains_sensitive_material(name)
    assert redact_line(name) == name

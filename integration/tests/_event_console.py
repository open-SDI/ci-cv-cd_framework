from __future__ import annotations

import json
import signal
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sdi_pipeline_integration._pipeline_event_check import check_events
from sdi_pipeline_integration._pipeline_events import PRISM_PREFIX, SDI_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sdi_pipeline_integration._pipeline_event_check import Golden

EVENT_PREFIXES = (SDI_PREFIX.encode(), PRISM_PREFIX.encode())


@dataclass
class Console:
    parts: list[bytes] = field(default_factory=list)

    @property
    def stderr(self) -> bytes:
        return b"".join(self.parts)

    def run(
        self, *arguments: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["sdi-integration", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.add(completed.stdout, completed.stderr)
        return completed

    def interrupt(
        self, *arguments: str, after: str
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            ["sdi-integration", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        seen: list[str] = []
        for line in process.stderr:
            seen.append(line)
            if after in line:
                process.send_signal(signal.SIGTERM)
                break
        stderr = "".join(seen) + process.stderr.read()
        stdout = process.stdout.read()
        process.wait(timeout=30)
        assert any(after in line for line in seen), stderr
        self.add(stdout, stderr)
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )

    def add(self, stdout: str, stderr: str) -> None:
        assert "PRISM_EVENT" not in stdout, stdout
        assert SDI_PREFIX not in stdout, stdout
        if stdout:
            json.loads(stdout)
        self.parts.append(stderr.encode("ascii"))


def _event_lines(stderr: bytes) -> list[bytes]:
    return [line for line in stderr.splitlines() if line.startswith(EVENT_PREFIXES)]


def _assert_only_events(stderr: bytes) -> None:
    if _event_lines(stderr):
        assert _event_lines(stderr) == stderr.splitlines(), stderr.decode()


def assert_conforms(console: Console, golden: Golden | None) -> None:
    stderr = console.stderr
    violations = check_events(stderr, golden)
    assert violations == [], "\n".join(
        [*(violation.render("stderr") for violation in violations), stderr.decode()]
    )
    for part in console.parts:
        _assert_only_events(part)
    if golden is not None:
        assert len(_event_lines(stderr)) == 2 * len(golden.lines), stderr.decode()


def assert_no_events(stderr: str | bytes) -> None:
    text = stderr.encode("ascii") if isinstance(stderr, str) else stderr
    assert b"PRISM_EVENT" not in text, text.decode()
    assert _event_lines(text) == [], text.decode()

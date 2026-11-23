"""Rows built from accepted attempts pass `check-events` for every golden."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._cli import main
from tests._pipeline_event_scenarios import SCENARIOS, golden_console
from tests._pipeline_events import GOLDEN_DIRECTORY, golden_names

if TYPE_CHECKING:
    from pathlib import Path


def test_every_golden_has_a_scenario() -> None:
    assert set(SCENARIOS) == set(golden_names())


@pytest.mark.parametrize("name", golden_names())
def test_rows_built_from_attempts_pass_check_events(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    console = tmp_path / f"{name}.console"
    console.write_bytes(golden_console(name))
    goldens = ["--goldens", str(GOLDEN_DIRECTORY)]

    golden = main(["check-events", str(console), "--golden", name, *goldens])
    real_run = main(["check-events", str(console), "--real-run"])

    assert (golden, real_run) == (0, 0), capsys.readouterr().out

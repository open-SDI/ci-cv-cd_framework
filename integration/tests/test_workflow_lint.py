"""Lint the GitHub workflows with actionlint before GitHub runs them."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
ACTIONLINT = Path(sys.executable).parent / "actionlint"
CONFIG = SOURCE_ROOT / ".github/actionlint.yaml"


def actionlint_failures(workflows: list[Path]) -> list[str]:
    assert ACTIONLINT.is_file(), (
        "actionlint is in the workflow-lint dependency group; run uv sync --all-groups"
    )
    completed = subprocess.run(
        [
            ACTIONLINT,
            "-oneline",
            "-shellcheck=",
            "-pyflakes=",
            "-config-file",
            CONFIG,
            *workflows,
        ],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode in {0, 1}, completed.stderr
    return [
        f"GitHub rejects this only at run time: {line}"
        for line in completed.stdout.splitlines()
    ]


def test_committed_workflows_pass_actionlint() -> None:
    workflows = sorted((SOURCE_ROOT / ".github/workflows").glob("*.yml"))
    assert workflows
    failures = actionlint_failures(workflows)
    assert not failures, "\n".join(failures)


def test_status_function_outside_an_if_condition_fails(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        """\
on: workflow_dispatch
jobs:
  report:
    runs-on: sdi-jenkins-handoff
    steps:
      - env:
          WORKFLOW_CANCELLED: ${{ cancelled() }}
        run: echo "$WORKFLOW_CANCELLED"
""",
        encoding="utf-8",
    )

    failures = actionlint_failures([workflow])

    assert len(failures) == 1
    assert failures[0].startswith("GitHub rejects this only at run time: ")
    assert "workflow.yml:7:" in failures[0]
    assert 'calling function "cancelled" is not allowed here' in failures[0]

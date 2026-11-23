"""`sdi-cv-warm` where no CV engine answers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_cv_warm_fails_when_the_cv_engine_does_not_answer() -> None:
    completed = subprocess.run(
        [Path(sys.executable).parent / "sdi-cv-warm"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stderr.startswith(
        "sdi-cv-warm: engine-unreachable: Error while fetching server API version"
    ), completed.stderr
    assert " built in " not in completed.stdout
    assert " present in " not in completed.stdout

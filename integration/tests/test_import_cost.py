"""Keep the CLI import free of client libraries that only some commands use."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("library", ["openai", "docker"])
def test_cli_import_does_not_load_client_library(library: str) -> None:
    probe = (
        f"import sys, sdi_pipeline_integration._cli; print({library!r} in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "False\n", (
        f"{library} loaded by import of _cli; "
        "import it inside the function that builds the client"
    )

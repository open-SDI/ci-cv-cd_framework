"""Keep the CLI import free of the OpenAI client library."""

from __future__ import annotations

import subprocess
import sys


def test_cli_import_does_not_load_openai() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, sdi_pipeline_integration._cli; print('openai' in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "False\n", (
        "openai loaded by import of _cli; "
        "import it inside the function that builds the client"
    )

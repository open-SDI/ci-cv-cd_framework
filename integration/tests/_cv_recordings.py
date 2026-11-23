from __future__ import annotations

import hashlib
from pathlib import Path

from sdi_pipeline_integration._cv_result import ScenarioResult

RECORDED = Path(__file__).parent / "data" / "cv-results"
NAMES = ("waffle-pass", "burger-pass", "no-face")


def manifest() -> dict[str, str]:
    lines = (RECORDED / "MANIFEST").read_text().splitlines()
    assert lines[0].startswith("# recorded by cv_engine session ")
    return {
        name: digest
        for digest, name in (line.split("  ", 1) for line in lines[1:] if line)
    }


def recorded(name: str) -> ScenarioResult:
    content = (RECORDED / f"{name}.json").read_bytes()
    assert hashlib.sha256(content).hexdigest() == manifest()[f"{name}.json"], name
    return ScenarioResult.model_validate_json(content)

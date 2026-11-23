"""Check the packaged CV simulation build context the docker SDK builds."""

from __future__ import annotations

import re
from importlib.resources import files

CONTEXT = files("sdi_pipeline_integration").joinpath("_cv_sim", "s-04")
BUILDKIT_ONLY = re.compile(r"^#\s*syntax\s*=|^RUN(\s+--\S+)*\s+--mount", re.IGNORECASE)


def _instructions() -> list[str]:
    dockerfile = CONTEXT.joinpath("Dockerfile").read_text()
    return [
        line.strip()
        for line in dockerfile.replace("\\\n", " ").splitlines()
        if line.strip()
    ]


def test_dockerfile_builds_with_the_classic_builder() -> None:
    assert [line for line in _instructions() if BUILDKIT_ONLY.search(line)] == []


def test_every_copy_source_is_in_the_packaged_context() -> None:
    sources = [
        source
        for line in _instructions()
        if line.upper().startswith("COPY ")
        for source in line.split()[1:-1]
        if not source.startswith("--")
    ]

    assert sources
    assert [
        source
        for source in sources
        if not (CONTEXT.joinpath(source).is_file() or CONTEXT.joinpath(source).is_dir())
    ] == []

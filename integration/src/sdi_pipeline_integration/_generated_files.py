"""Writing and freshness checks for committed files generated from code."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def write_generated(directory: Path, files: Mapping[str, bytes]) -> None:
    """Write every generated file; other files in the directory are kept."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (directory / filename).write_bytes(content)


def check_generated(
    directory: Path,
    pattern: str,
    files: Mapping[str, bytes],
    *,
    singular: str,
    plural: str,
) -> None:
    """Reject missing, obsolete, or stale generated files matching `pattern`."""
    actual_names = {path.name for path in directory.glob(pattern)}
    if actual_names != set(files):
        missing = sorted(set(files) - actual_names)
        obsolete = sorted(actual_names - set(files))
        msg = f"{singular} set is stale; missing={missing}, obsolete={obsolete}"
        raise InputError(msg)
    stale = [
        filename
        for filename, content in files.items()
        if (directory / filename).read_bytes() != content
    ]
    if stale:
        msg = f"generated {plural} are stale: {sorted(stale)}"
        raise InputError(msg)

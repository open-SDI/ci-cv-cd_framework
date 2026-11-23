"""Identify the integration code a Jenkins agent image bakes in.

An agent image installs the integration package when it is built, so a run
executes that code rather than the code at its submitted commit. The image
records a digest of the baked code, and each run compares it with the same
digest of its checkout before any Stage work.
"""

from __future__ import annotations

import hashlib
import re
import stat
from typing import TYPE_CHECKING

from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

# The paths the agent Dockerfile copies into the image, besides README.md,
# which only feeds package metadata.
BAKED_ROOTS = ("pyproject.toml", "uv.lock", "src", "fixtures")
STALE_IMAGE_REASON = "sdi.agent.stale-image"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REBUILD = (
    "rebuild the agent images from a clean checkout (Safe Shutdown, then stack start)"
)


class StaleAgentImageError(InputError):
    """Signal agents whose images bake code other than the submitted commit's."""


def _is_build_cache(name: str) -> bool:
    return name == "__pycache__" or name.endswith(".pyc")


def _baked_files(root: Path) -> Iterator[tuple[str, Path]]:
    for baked_root in BAKED_ROOTS:
        path = root / baked_root
        if not path.exists() and not path.is_symlink():
            msg = f"the baked integration code has no {baked_root}"
            raise InputError(msg)
        pending = [path]
        while pending:
            current = pending.pop()
            relative = current.relative_to(root).as_posix()
            mode = current.lstat().st_mode
            if stat.S_ISDIR(mode):
                pending.extend(
                    child
                    for child in current.iterdir()
                    if not _is_build_cache(child.name)
                )
            elif stat.S_ISREG(mode):
                yield relative, current
            else:
                msg = f"the baked integration code has a non-regular file {relative}"
                raise InputError(msg)


def integration_code_digest(root: Path) -> str:
    """Return the SHA-256 of a manifest of every baked file under one root."""
    manifest = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n"
        for relative, path in sorted(_baked_files(root))
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


def _shown(value: str) -> str:
    return value[:12] if _DIGEST.fullmatch(value) else "missing"


def require_fresh_agent_images(
    expected: str,
    baked: Mapping[str, str],
    *,
    integration_node: str | None = None,
) -> None:
    """Reject agents whose recorded digest differs from the checkout's.

    Each agent is named by its Jenkins node. When the integration agent is
    stale, its own digest of the checkout cannot be trusted, so the other
    agents are not judged.
    """
    unchecked = ""
    if integration_node is not None and baked[integration_node] != expected:
        stale = {integration_node: baked[integration_node]}
        unchecked = "; the other agents were not checked"
    else:
        stale = {node: value for node, value in baked.items() if value != expected}
    if not stale:
        return
    if len(stale) == 1:
        subject = f"the {next(iter(stale))} agent image was"
    else:
        subject = f"agent images {', '.join(stale)} were"
    recorded = ", ".join(f"{node} {_shown(value)}" for node, value in stale.items())
    msg = (
        f"{STALE_IMAGE_REASON}: {subject} built from integration code other than "
        f"the submitted commit's (commit {expected[:12]}, recorded {recorded})"
        f"{unchecked}; {_REBUILD}"
    )
    raise StaleAgentImageError(msg)

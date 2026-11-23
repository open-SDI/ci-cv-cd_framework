"""Redact build-log text before it reaches a result or diagnostic.

The runtime rejects a whole candidate when any output string or the diagnostic
matches its sensitive-material check, and BuildKit and apt logs are full of
URLs, machine paths and private addresses. Each line is masked with the
runtime's own patterns and then checked by the runtime's own predicate, so the
two rules cannot drift.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._stage_contracts import (
    IPV4_PATTERN,
    IPV6_PATTERN,
    POSIX_MACHINE_PATH_PATTERN,
    PRIVATE_HOSTNAME_PATTERN,
    contains_sensitive_material,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

MAX_DETAIL_CHARACTERS = 256
# A long last error line keeps its start and its end, where the exit code is.
DETAIL_HEAD_CHARACTERS = 100
ELISION = "..."
# Lines shrink when masked, so the tail reads more raw bytes than it keeps.
RAW_TAIL_FACTOR = 4
RAW_TAIL_MINIMUM_BYTES = 64 * 1024
ANSI_PATTERN = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
)
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
URL_PATTERN = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://\S*")
WINDOWS_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]\S*")
LOCALHOST_PATTERN = re.compile(r"localhost", re.IGNORECASE)
LINE_BREAK_PATTERN = re.compile(r"\r\n|\r|\n")
# Printable ASCII without the quote and backslash a summary would have to escape.
DETAIL_EXCLUDED_PATTERN = re.compile(r"[^\x20\x21\x23-\x5b\x5d-\x7e]")


def _mask_address(match: re.Match[str]) -> str:
    try:
        address = ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    if address.is_private or address.is_loopback or address.is_link_local:
        return "<address>"
    return match.group(0)


def redact_line(line: str) -> str | None:
    """Return the line with addresses and paths masked, or None to drop it."""
    text = ANSI_PATTERN.sub("", line).replace("\t", " ")
    text = CONTROL_PATTERN.sub("", text)
    text = URL_PATTERN.sub("<url>", text)
    text = POSIX_MACHINE_PATH_PATTERN.sub("<path>", text)
    text = WINDOWS_PATH_PATTERN.sub("<path>", text)
    text = PRIVATE_HOSTNAME_PATTERN.sub("<host>", text)
    text = LOCALHOST_PATTERN.sub("<host>", text)
    text = IPV4_PATTERN.sub(_mask_address, text)
    text = IPV6_PATTERN.sub(_mask_address, text).rstrip()
    # Credential forms cannot be masked without guessing their extent.
    if contains_sensitive_material(text):
        return None
    return text


@dataclass(frozen=True)
class LogTail:
    """The last redacted log lines, and whether earlier ones were left out."""

    lines: list[str]
    cut: bool


def log_tail(path: Path, *, max_bytes: int) -> LogTail:
    """Return the last redacted, non-empty log lines within a byte budget."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(
                0, size - max(max_bytes * RAW_TAIL_FACTOR, RAW_TAIL_MINIMUM_BYTES)
            )
            stream.seek(start)
            raw = stream.read()
    except OSError:
        return LogTail([], cut=False)
    lines = LINE_BREAK_PATTERN.split(raw.decode("utf-8", errors="replace"))
    if start > 0:
        # The first line read may have been cut mid-line.
        lines = lines[1:]
    tail = tail_lines(lines, max_bytes=max_bytes)
    return LogTail(tail.lines, cut=tail.cut or start > 0)


def tail_lines(lines: Sequence[str], *, max_bytes: int) -> LogTail:
    kept: list[str] = []
    used = 0
    cut = False
    for line in reversed(lines):
        redacted = redact_line(line)
        if not redacted:
            continue
        cost = len(redacted.encode()) + 1
        if used + cost > max_bytes:
            cut = True
            break
        kept.append(redacted)
        used += cost
    kept.reverse()
    return LogTail(kept, cut=cut)


def failure_text(text: str, *, fallback: str) -> str:
    redacted = redact_line(text)
    detail = "" if redacted is None else _reduce(redacted)
    if not detail or contains_sensitive_material(detail):
        return fallback
    return detail


def _reduce(line: str) -> str:
    text = DETAIL_EXCLUDED_PATTERN.sub("", line).strip()
    if len(text) > MAX_DETAIL_CHARACTERS:
        tail = MAX_DETAIL_CHARACTERS - DETAIL_HEAD_CHARACTERS - len(ELISION)
        text = text[:DETAIL_HEAD_CHARACTERS] + ELISION + text[-tail:]
    return text


def failure_detail(path: Path, *, fallback: str) -> str:
    """Return the redacted last log line as a result detail, or the fallback."""
    for line in reversed(log_tail(path, max_bytes=RAW_TAIL_MINIMUM_BYTES).lines):
        if _reduce(line):
            return failure_text(line, fallback=fallback)
    return fallback

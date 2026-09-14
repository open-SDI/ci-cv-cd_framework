"""Immutable Git-object access for repository-owned inputs."""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from ._yaml_input import MAX_INPUT_BYTES, InputError

if TYPE_CHECKING:
    from pathlib import Path

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
GITHUB_HTTPS_OR_SSH = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repository>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
REGULAR_BLOB_MODES = {"100644", "100755"}
ASCII_CONTROL_LIMIT = 32
ASCII_DELETE = 127


@dataclass(frozen=True)
class CommittedBlob:
    """Exact bytes and metadata for one regular file in a commit tree."""

    path: str
    content: bytes


class GitRepository:
    """Read regular files from one immutable commit without checkout access."""

    def __init__(self, repository: Path, commit_sha: str) -> None:
        self._repository = repository.resolve(strict=True)
        if not FULL_SHA.fullmatch(commit_sha):
            msg = "resolved commit must be a lowercase full commit SHA"
            raise InputError(msg)
        resolved = self._git_text(
            "rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{commit}}"
        )
        if resolved != commit_sha:
            msg = "resolved commit does not identify the requested commit object"
            raise InputError(msg)
        self.commit_sha = commit_sha
        self.identity = self._repository_identity()

    def _git(self, *arguments: str) -> bytes:
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": "/nonexistent",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        command = [
            "git",
            "--no-replace-objects",
            "--no-lazy-fetch",
            "--literal-pathspecs",
            "-C",
            str(self._repository),
            *arguments,
        ]
        try:
            completed = subprocess.run(  # noqa: S603
                command,
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                env=environment,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            msg = f"Git operation failed: {' '.join(arguments)}"
            raise InputError(msg) from error
        return completed.stdout

    def _git_text(self, *arguments: str) -> str:
        try:
            return self._git(*arguments).decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as error:
            msg = "Git returned invalid UTF-8 metadata"
            raise InputError(msg) from error

    def _repository_identity(self) -> str:
        values = self._git(
            "config", "--local", "--null", "--get-all", "remote.origin.url"
        )
        try:
            urls = [
                item.decode("utf-8", errors="strict")
                for item in values.split(b"\0")
                if item
            ]
        except UnicodeDecodeError as error:
            msg = "origin URL is not valid UTF-8"
            raise InputError(msg) from error
        if len(urls) != 1:
            msg = "repository must have exactly one origin URL"
            raise InputError(msg)
        match = GITHUB_HTTPS_OR_SSH.fullmatch(urls[0])
        if match is None:
            msg = "origin must be an uncredentialed GitHub repository URL"
            raise InputError(msg)
        repository = match.group("repository")
        return f"github.com/{match.group('owner')}/{repository}"

    def require_ref_commit(self, requested_ref: str) -> None:
        """Require the immutable commit to be the commit named by the ref."""
        ref_commit = self._git_text(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{requested_ref}^{{commit}}",
        )
        if ref_commit != self.commit_sha:
            msg = f"resolved commit does not match {requested_ref}"
            raise InputError(msg)

    def read_regular_file(self, path: str) -> CommittedBlob:
        """Return exact committed bytes after lexical and tree-entry checks."""
        validate_repository_path(path)
        entry = self._git("ls-tree", "--full-tree", "-z", self.commit_sha, "--", path)
        records = [record for record in entry.split(b"\0") if record]
        if len(records) != 1:
            msg = f"{path}: path must identify one committed regular file"
            raise InputError(msg)
        try:
            metadata, returned_path = records[0].split(b"\t", maxsplit=1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            decoded_path = returned_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            msg = f"{path}: malformed Git tree entry"
            raise InputError(msg) from error
        if decoded_path != path:
            msg = f"{path}: Git tree path does not match the requested path"
            raise InputError(msg)
        if mode not in REGULAR_BLOB_MODES or object_type != "blob":
            msg = f"{path}: path is not a committed regular file"
            raise InputError(msg)
        size_text = self._git_text("cat-file", "-s", object_id)
        try:
            size = int(size_text)
        except ValueError as error:
            msg = f"{path}: Git returned an invalid blob size"
            raise InputError(msg) from error
        if size > MAX_INPUT_BYTES:
            msg = f"{path}: file exceeds {MAX_INPUT_BYTES} bytes"
            raise InputError(msg)
        content = self._git("cat-file", "blob", object_id)
        if len(content) != size:
            msg = f"{path}: Git blob size changed while reading"
            raise InputError(msg)
        return CommittedBlob(path=path, content=content)


def validate_repository_path(path: str) -> None:
    """Require one normalized, repository-relative POSIX path."""
    if not path or unicodedata.normalize("NFC", path) != path:
        msg = "repository path must be non-empty normalized Unicode"
        raise InputError(msg)
    if any(
        ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
        for character in path
    ):
        msg = "repository path must not contain control characters"
        raise InputError(msg)
    if "\\" in path or PureWindowsPath(path).is_absolute():
        msg = "repository path must use relative POSIX syntax"
        raise InputError(msg)
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or parsed.as_posix() != path:
        msg = "repository path must be normalized and repository-relative"
        raise InputError(msg)
    if any(part in {"", ".", ".."} for part in path.split("/")):
        msg = "repository path must not contain empty, dot, or parent components"
        raise InputError(msg)

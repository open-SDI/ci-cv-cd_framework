"""Fail when a committed Markdown file names a repository path that is gone."""

from __future__ import annotations

import posixpath
import re
import subprocess
import tempfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
CODE_SPAN = re.compile(r"`([^`\s]+)`")
LINK_TARGET = re.compile(r"\]\(([^)\s#]+)")
LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
NOT_A_PATH = re.compile(r"://|[*?<>{}\[\]$:@=~|]|^-")


def _tracked(root: Path) -> set[str]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {name for name in listed.split("\0") if name}


def _ignored(root: Path, paths: set[str]) -> set[str]:
    with tempfile.TemporaryDirectory() as git_dir:
        subprocess.run(
            ["git", "init", "-q", "--bare", "--template=", git_dir], check=True
        )
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={root}",
                "-c",
                "core.excludesFile=/dev/null",
                "check-ignore",
                "--no-index",
                "--stdin",
            ],
            input="\n".join(sorted(paths)),
            capture_output=True,
            text=True,
            check=False,
            cwd=root,
        )
    if completed.returncode not in {0, 1}:
        raise RuntimeError(completed.stderr)
    return set(completed.stdout.splitlines())


def _with_parents(files: set[str]) -> set[str]:
    entries = set(files)
    for name in files:
        parent = posixpath.dirname(name)
        while parent and parent not in entries:
            entries.add(parent)
            parent = posixpath.dirname(parent)
    return entries


def _directories_up_from(document: str) -> list[str]:
    directories = [posixpath.dirname(document)]
    while directories[-1]:
        directories.append(posixpath.dirname(directories[-1]))
    return directories


def _candidates(
    document: str, token: str, entries: set[str], *, link: bool
) -> set[str]:
    path = LINE_SUFFIX.sub("", token).rstrip("/")
    if not path or NOT_A_PATH.search(path):
        return set()
    if path.startswith("/"):
        return {posixpath.normpath(path.lstrip("/"))} if link else set()
    directory = posixpath.dirname(document)
    first = path.split("/", 1)[0]
    if link or first in {".", ".."}:
        return {posixpath.normpath(posixpath.join(directory, path))}
    if "/" not in path:
        return set()
    bases = [
        base
        for base in _directories_up_from(document)
        if posixpath.join(base, first) in entries
    ]
    if not bases and path.endswith(".md"):
        bases = [directory, ""]
    return {posixpath.normpath(posixpath.join(base, path)) for base in bases}


def missing_paths(root: Path) -> list[str]:
    tracked = _tracked(root)
    entries = _with_parents(tracked)
    named: list[tuple[str, set[str]]] = []
    for document in sorted(tracked):
        if not document.endswith(".md") or document.startswith("CV/gazebo/models/"):
            continue
        lines = (root / document).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            tokens = [(token, False) for token in CODE_SPAN.findall(line)]
            tokens += [(token, True) for token in LINK_TARGET.findall(line)]
            for token, link in tokens:
                candidates = _candidates(document, token, entries, link=link)
                if candidates and entries.isdisjoint(candidates):
                    named.append((f"{document}:{number}: {token}", candidates))
    inside = {
        path
        for _, paths in named
        for path in paths
        if path != ".." and not path.startswith("../")
    }
    ignored = _ignored(root, inside)
    return [where for where, paths in named if ignored.isdisjoint(paths)]


def test_committed_markdown_names_only_existing_paths() -> None:
    missing = missing_paths(SOURCE_ROOT)

    assert not missing, (
        "Markdown names a repository path that does not exist; "
        "correct or delete each reference:\n" + "\n".join(missing)
    )


def test_a_missing_path_is_reported_with_its_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "docs/config").mkdir(parents=True)
    (tmp_path / "docs/config/example.env").write_text("KEY=\n", encoding="utf-8")
    (tmp_path / "docs/guide.md").write_text(
        "See `docs/guide.md` and [the notes](notes.md).\n"
        "Copy it to the ignored `config/local/settings.env` first.\n"
        "Read `docs/gone.md` and [the setup](../setup/README.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/notes.md").write_text("Notes.\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("/docs/config/local/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)

    assert missing_paths(tmp_path) == [
        "docs/guide.md:3: docs/gone.md",
        "docs/guide.md:3: ../setup/README.md",
    ]


def test_project_relative_rooted_and_unknown_paths_are_checked(
    tmp_path: Path,
) -> None:
    (tmp_path / "integration/src").mkdir(parents=True)
    (tmp_path / "integration/src/present.py").write_text("", encoding="utf-8")
    (tmp_path / "integration/README.md").write_text(
        "Edit `src/present.py:12` and `src/missing.py:3-9`.\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "Read `gone/README.md`, `integraton/README.md`, and [docs](/docs/missing.md).\n"
        "See [the sibling](../sibling/README.md) and the ignored `docs/local/x.env`.\n"
        "Read [the guide](/integration/README.md) and the ignored `docs/local/z.env`.",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/kept.md").write_text("Kept.\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("/docs/local/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    (tmp_path / ".git/info/exclude").write_text("gone/\n", encoding="utf-8")

    assert missing_paths(tmp_path) == [
        "README.md:1: gone/README.md",
        "README.md:1: integraton/README.md",
        "README.md:1: /docs/missing.md",
        "README.md:2: ../sibling/README.md",
        "integration/README.md:1: src/missing.py:3-9",
    ]

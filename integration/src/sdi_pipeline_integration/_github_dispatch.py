"""Sequential GitHub workflow dispatch for the reviewed S-04 Fixture requests."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ._json_input import parse_json
from ._yaml_input import InputError

if TYPE_CHECKING:
    from typing import TextIO

WORKFLOW_FILE = "pipeline-integration.yml"
GITHUB_API_VERSION = "2026-03-10"
MAX_DISPATCH_RESPONSE_BYTES = 64 * 1024
S04_FIXTURE_REQUESTS = tuple(
    f"runs/s-04/s-04-tc-03-c-{number:02d}-fixture.yaml" for number in range(1, 7)
)
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$"
)


@dataclass(frozen=True)
class GitHubDispatchError(Exception):
    """A sanitized GitHub CLI dispatch or observation failure."""

    message: str

    def __str__(self) -> str:
        return self.message


def _dispatch(repository: str, run_request_path: str) -> tuple[int, str]:
    request = json.dumps(
        {
            "ref": "main",
            "inputs": {"run_request_path": run_request_path},
        },
        separators=(",", ":"),
    )
    try:
        completed = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "gh",
                "api",
                "--method",
                "POST",
                "--header",
                "Accept: application/vnd.github+json",
                "--header",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                f"repos/{repository}/actions/workflows/{WORKFLOW_FILE}/dispatches",
                "--input",
                "-",
            ],
            check=False,
            capture_output=True,
            input=request,
            text=True,
        )
    except OSError as error:
        msg = "GitHub CLI could not be executed"
        raise GitHubDispatchError(msg) from error
    if completed.returncode != 0:
        msg = "GitHub workflow dispatch failed"
        raise GitHubDispatchError(msg)
    response = parse_json(
        completed.stdout.encode(),
        "GitHub workflow dispatch response",
        max_bytes=MAX_DISPATCH_RESPONSE_BYTES,
    )
    run_id = response.get("workflow_run_id")
    html_url = response.get("html_url")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        msg = "GitHub workflow dispatch did not return a valid workflow run ID"
        raise GitHubDispatchError(msg)
    if not isinstance(html_url, str):
        msg = "GitHub workflow dispatch did not return an exact run URL"
        raise GitHubDispatchError(msg)
    split_url = urlsplit(html_url)
    expected_path = f"/{repository}/actions/runs/{run_id}"
    if (
        split_url.scheme != "https"
        or split_url.hostname != "github.com"
        or split_url.username is not None
        or split_url.password is not None
        or split_url.port is not None
        or split_url.path.casefold() != expected_path.casefold()
        or split_url.query
        or split_url.fragment
    ):
        msg = "GitHub workflow dispatch returned an invalid run URL"
        raise GitHubDispatchError(msg)
    return run_id, html_url


def dispatch_s04(
    repository: str,
    *,
    url_output: TextIO,
    diagnostics: TextIO,
) -> bool:
    """Dispatch and wait for the six reviewed requests without aggregation."""
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        msg = "GitHub repository must use the owner/name form"
        raise InputError(msg)
    all_succeeded = True
    for run_request_path in S04_FIXTURE_REQUESTS:
        run_id, html_url = _dispatch(repository, run_request_path)
        url_output.write(f"{html_url}\n")
        url_output.flush()
        try:
            watched = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "gh",
                    "run",
                    "watch",
                    str(run_id),
                    "--repo",
                    repository,
                    "--exit-status",
                ],
                check=False,
                stdout=diagnostics,
                stderr=diagnostics,
                text=True,
            )
        except OSError as error:
            msg = "GitHub CLI could not observe the exact workflow run"
            raise GitHubDispatchError(msg) from error
        if watched.returncode != 0:
            all_succeeded = False
            break
    return all_succeeded

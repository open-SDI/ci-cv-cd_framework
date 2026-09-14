"""Deep GitHub-to-Jenkins handoff, correlation, and retrieval operation."""

# Structured failure codes are intentionally constructed at each rejection site.
# ruff: noqa: EM101, TRY300, TRY301

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, final
from urllib.parse import quote, urljoin, urlsplit

import requests
from pydantic import ValidationError
from requests.adapters import HTTPAdapter

from ._handoff_contracts import (
    HANDOFF_CONTRACT_VERSION,
    HANDOFF_RECEIPT_SCHEMA_VERSION,
    FailureCode,
    HandoffPhase,
    HandoffReceipt,
)
from ._json_input import parse_json
from ._local_dispatch import MAX_RESULT_BYTES, parse_pipeline_result, validate_bundle
from ._result_contracts import PIPELINE_RESULT_PATH, PipelineIntegrationResult
from ._run_input import identify_committed_run
from ._stage_runtime import ExternalCancellation
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Mapping
    from types import FrameType

DEFAULT_HTTP_LIMIT_SECONDS = 30
DEFAULT_SUBMISSION_LIMIT_SECONDS = 30
DEFAULT_QUEUE_WAIT_LIMIT_SECONDS = 15 * 60
DEFAULT_JENKINS_EXECUTION_LIMIT_SECONDS = 90 * 60
DEFAULT_RETRIEVAL_LIMIT_SECONDS = 5 * 60
DEFAULT_RUNNER_OUTER_LIMIT_SECONDS = 100 * 60
DEFAULT_GITHUB_JOB_LIMIT_SECONDS = 110 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
MAX_LIMIT_SECONDS = 24 * 60 * 60
MAX_STATUS_BYTES = 64 * 1024
MAX_POLL_INTERVAL_SECONDS = 60
ASCII_CONTROL_LIMIT = 32
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
JOB_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]{0,19}$")
JENKINS_RESULTS = {"SUCCESS", "FAILURE", "ABORTED", "UNSTABLE", "NOT_BUILT"}
type JsonObject = dict[str, object]


@dataclass(frozen=True)
class HandoffSettings:
    """Trusted deployment settings and protected secret delivery."""

    base_url: str
    job_path: str
    repository: str
    username: str
    api_token: str
    http_limit_seconds: int
    submission_limit_seconds: int
    queue_wait_limit_seconds: int
    jenkins_execution_limit_seconds: int
    retrieval_limit_seconds: int
    runner_outer_limit_seconds: int
    github_job_limit_seconds: int
    poll_interval_seconds: float


@dataclass(frozen=True)
class RequestBudget:
    """One HTTP operation nested within phase and runner deadlines."""

    operation_started: float
    phase_started: float
    phase_limit: int | None
    phase_code: FailureCode | None
    last_known_state: HandoffPhase


@dataclass(frozen=True)
class JenkinsResponse:
    """A streaming response carrying its complete operation budget."""

    response: requests.Response
    budget: RequestBudget


class _ResponseBody(Protocol):
    def read1(self, amount: int, *, decode_content: bool) -> bytes: ...


@final
class HandoffError(Exception):
    """A sanitized terminal handoff failure."""

    def __init__(  # noqa: PLR0913
        self,
        code: FailureCode,
        phase: HandoffPhase,
        *,
        limit_seconds: int | None = None,
        elapsed_seconds: float | None = None,
        last_known_state: HandoffPhase | None = None,
        jenkins_result: str | None = None,
        result: JsonObject | None = None,
    ) -> None:
        super().__init__(code)
        self.code: FailureCode = code
        self.phase: HandoffPhase = phase
        self.limit_seconds: int | None = limit_seconds
        self.elapsed_seconds: float | None = elapsed_seconds
        self.last_known_state: HandoffPhase | None = last_known_state
        self.jenkins_result: str | None = jenkins_result
        self.result: JsonObject | None = result


@final
class JenkinsRequestError(Exception):
    """A request failed without exposing transport diagnostics."""

    def __init__(self, *, timed_out: bool = False, elapsed_seconds: float = 0) -> None:
        super().__init__("Jenkins HTTP operation failed")
        self.timed_out: bool = timed_out
        self.elapsed_seconds: float = elapsed_seconds


def _failure(  # noqa: PLR0913
    code: FailureCode,
    phase: HandoffPhase,
    *,
    limit_seconds: int | None = None,
    elapsed_seconds: float | None = None,
    last_known_state: HandoffPhase | None = None,
    jenkins_result: str | None = None,
    result: JsonObject | None = None,
) -> HandoffError:
    return HandoffError(
        code,
        phase,
        limit_seconds=limit_seconds,
        elapsed_seconds=elapsed_seconds,
        last_known_state=last_known_state,
        jenkins_result=jenkins_result,
        result=result,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


@final
class _ReceiptWriter:
    def __init__(
        self,
        path: Path,
        *,
        execution_id: str,
        github_run_id: int,
        github_run_attempt: int,
        progress: Callable[[str], None],
    ) -> None:
        if path.exists() or path.is_symlink():
            msg = "receipt path must not already exist"
            raise InputError(msg)
        self._path: Path = path
        self._progress: Callable[[str], None] = progress
        self._document: JsonObject = {
            "schema_version": HANDOFF_RECEIPT_SCHEMA_VERSION,
            "execution_id": execution_id,
            "github": {
                "run_id": github_run_id,
                "run_attempt": github_run_attempt,
            },
            "phase": "validating",
            "transitions": [{"phase": "validating", "observed_at": _now().isoformat()}],
        }
        self._write()
        progress("validating")

    def _write(self) -> None:
        receipt = HandoffReceipt.model_validate(
            self._document,
            strict=True,
            extra="forbid",
        )
        content = _canonical_json(receipt.model_dump(mode="json", exclude_none=True))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            dir=self._path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                _ = stream.write(content)
                stream.flush()
                _ = os.fsync(stream.fileno())
            _ = temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def transition(
        self,
        phase: HandoffPhase,
        *,
        queue_id: int | None = None,
        build_number: int | None = None,
        job_relative: str | None = None,
    ) -> None:
        self._document["phase"] = phase
        cast("list[JsonObject]", self._document["transitions"]).append(
            {"phase": phase, "observed_at": _now().isoformat()}
        )
        if queue_id is not None:
            self._document["queue"] = {
                "queue_id": queue_id,
                "relative_url": f"queue/item/{queue_id}/",
            }
        if build_number is not None and job_relative is not None:
            self._document["build"] = {
                "build_number": build_number,
                "relative_url": f"{job_relative}{build_number}/",
            }
        self._write()
        self._progress(phase)

    def fail(self, error: HandoffError) -> None:
        observed_at = _now().isoformat()
        self._document["phase"] = "failed"
        cast("list[JsonObject]", self._document["transitions"]).append(
            {"phase": "failed", "observed_at": observed_at}
        )
        terminal: dict[str, object] = {
            "outcome": "failed",
            "observed_at": observed_at,
            "code": error.code,
            "phase": error.phase,
        }
        terminal.update(
            {
                name: value
                for name, value in (
                    ("limit_seconds", error.limit_seconds),
                    ("elapsed_seconds", error.elapsed_seconds),
                    ("last_known_state", error.last_known_state),
                    ("jenkins_result", error.jenkins_result),
                )
                if value is not None
            }
        )
        self._document["terminal"] = terminal
        self._write()
        self._progress("failed")

    def cancel(self, jenkins_result: str | None) -> None:
        observed_at = _now().isoformat()
        self._document["phase"] = "cancelled"
        cast("list[JsonObject]", self._document["transitions"]).append(
            {"phase": "cancelled", "observed_at": observed_at}
        )
        terminal: dict[str, object] = {
            "outcome": "cancelled",
            "observed_at": observed_at,
        }
        if jenkins_result in JENKINS_RESULTS:
            terminal["jenkins_result"] = jenkins_result
        self._document["terminal"] = terminal
        self._write()
        self._progress("cancelled")

    def succeed(self) -> None:
        observed_at = _now().isoformat()
        self._document["phase"] = "completed"
        cast("list[JsonObject]", self._document["transitions"]).append(
            {"phase": "completed", "observed_at": observed_at}
        )
        self._document["terminal"] = {
            "outcome": "succeeded",
            "observed_at": observed_at,
            "jenkins_result": "SUCCESS",
        }
        self._write()
        self._progress("completed")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if (
        not value
        or value.strip() != value
        or any(ord(character) < ASCII_CONTROL_LIMIT for character in value)
    ):
        msg = f"trusted handoff setting {name} is missing or invalid"
        raise InputError(msg)
    return value


def _limit(environment: Mapping[str, str], name: str, default: int) -> int:
    value = environment.get(name, str(default))
    if POSITIVE_DECIMAL.fullmatch(value) is None:
        msg = f"trusted handoff setting {name} must be a positive integer"
        raise InputError(msg)
    parsed = int(value)
    if parsed > MAX_LIMIT_SECONDS:
        msg = f"trusted handoff setting {name} must not exceed one day"
        raise InputError(msg)
    return parsed


def load_handoff_settings(environment: Mapping[str, str]) -> HandoffSettings:
    """Load and validate the trusted deployment and protected secret boundary."""
    base_url = _required(environment, "SDI_JENKINS_BASE_URL").rstrip("/")
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        msg = "trusted Jenkins base URL is invalid"
        raise InputError(msg)
    if parsed_url.scheme == "http" and parsed_url.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        msg = "plaintext Jenkins base URL must use the loopback interface"
        raise InputError(msg)

    job_path = _required(environment, "SDI_JENKINS_JOB_PATH")
    if any(
        JOB_COMPONENT.fullmatch(component) is None for component in job_path.split("/")
    ):
        msg = "trusted Jenkins job path is invalid"
        raise InputError(msg)
    username = _required(environment, "SDI_JENKINS_USERNAME")
    if ":" in username:
        msg = "trusted Jenkins machine username is invalid"
        raise InputError(msg)
    token = _required(environment, "SDI_JENKINS_API_TOKEN")

    http_limit = _limit(
        environment, "SDI_HANDOFF_HTTP_LIMIT_SECONDS", DEFAULT_HTTP_LIMIT_SECONDS
    )
    submission_limit = _limit(
        environment,
        "SDI_HANDOFF_SUBMISSION_LIMIT_SECONDS",
        DEFAULT_SUBMISSION_LIMIT_SECONDS,
    )
    queue_limit = _limit(
        environment,
        "SDI_HANDOFF_QUEUE_WAIT_LIMIT_SECONDS",
        DEFAULT_QUEUE_WAIT_LIMIT_SECONDS,
    )
    execution_limit = _limit(
        environment,
        "SDI_HANDOFF_JENKINS_EXECUTION_LIMIT_SECONDS",
        DEFAULT_JENKINS_EXECUTION_LIMIT_SECONDS,
    )
    retrieval_limit = _limit(
        environment,
        "SDI_HANDOFF_RETRIEVAL_LIMIT_SECONDS",
        DEFAULT_RETRIEVAL_LIMIT_SECONDS,
    )
    outer_limit = _limit(
        environment,
        "SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS",
        DEFAULT_RUNNER_OUTER_LIMIT_SECONDS,
    )
    github_limit = _limit(
        environment,
        "SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS",
        DEFAULT_GITHUB_JOB_LIMIT_SECONDS,
    )
    if github_limit <= outer_limit:
        msg = "GitHub job limit must exceed the runner outer handoff limit"
        raise InputError(msg)
    try:
        poll_interval = float(
            environment.get(
                "SDI_HANDOFF_POLL_INTERVAL_SECONDS",
                str(DEFAULT_POLL_INTERVAL_SECONDS),
            )
        )
    except ValueError as error:
        msg = "trusted handoff poll interval must be a positive number"
        raise InputError(msg) from error
    if not 0 < poll_interval <= MAX_POLL_INTERVAL_SECONDS:
        msg = "trusted handoff poll interval must be between zero and 60 seconds"
        raise InputError(msg)
    return HandoffSettings(
        base_url=base_url,
        job_path=job_path,
        repository=_required(environment, "SDI_JENKINS_REPOSITORY"),
        username=username,
        api_token=token,
        http_limit_seconds=http_limit,
        submission_limit_seconds=submission_limit,
        queue_wait_limit_seconds=queue_limit,
        jenkins_execution_limit_seconds=execution_limit,
        retrieval_limit_seconds=retrieval_limit,
        runner_outer_limit_seconds=outer_limit,
        github_job_limit_seconds=github_limit,
        poll_interval_seconds=poll_interval,
    )


@final
class _JenkinsClient:
    def __init__(self, settings: HandoffSettings, outer_started: float) -> None:
        self.settings: HandoffSettings = settings
        self.outer_started: float = outer_started
        self.job_relative: str = "".join(
            f"job/{quote(component, safe='')}/"
            for component in settings.job_path.split("/")
        )
        self.session: requests.Session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (settings.username, settings.api_token)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "sdi-pipeline-integration-handoff/1",
            }
        )
        adapter = HTTPAdapter(max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def close(self) -> None:
        self.session.close()

    def remaining(self, budget: RequestBudget) -> float:
        current = time.monotonic()
        outer_elapsed = current - self.outer_started
        if outer_elapsed >= self.settings.runner_outer_limit_seconds:
            raise _failure(
                "outer_deadline",
                budget.last_known_state,
                limit_seconds=self.settings.runner_outer_limit_seconds,
                elapsed_seconds=outer_elapsed,
                last_known_state=budget.last_known_state,
            )
        remaining = self.settings.runner_outer_limit_seconds - outer_elapsed
        if budget.phase_limit is not None:
            phase_elapsed = current - budget.phase_started
            if phase_elapsed >= budget.phase_limit:
                if budget.phase_code is None:
                    raise AssertionError
                raise _failure(
                    budget.phase_code,
                    budget.last_known_state,
                    limit_seconds=budget.phase_limit,
                    elapsed_seconds=phase_elapsed,
                    last_known_state=budget.last_known_state,
                )
            remaining = min(remaining, budget.phase_limit - phase_elapsed)
        operation_elapsed = current - budget.operation_started
        if operation_elapsed >= self.settings.http_limit_seconds:
            raise _failure(
                "http_operation",
                budget.last_known_state,
                limit_seconds=self.settings.http_limit_seconds,
                elapsed_seconds=operation_elapsed,
                last_known_state=budget.last_known_state,
            )
        return min(
            float(self.settings.http_limit_seconds) - operation_elapsed,
            remaining,
        )

    @contextmanager
    def blocking_deadline(self, budget: RequestBudget) -> Generator[None, None, None]:
        timeout = self.remaining(budget)

        def expire(_signal_number: int, _frame: FrameType | None) -> None:
            _ = self.remaining(budget)
            raise _failure(
                "http_operation",
                budget.last_known_state,
                limit_seconds=self.settings.http_limit_seconds,
                elapsed_seconds=time.monotonic() - budget.operation_started,
                last_known_state=budget.last_known_state,
            )

        previous_handler = signal.signal(signal.SIGALRM, expire)
        _ = signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            yield
        finally:
            _ = signal.setitimer(signal.ITIMER_REAL, 0)
            _ = signal.signal(signal.SIGALRM, previous_handler)

    def request(  # noqa: PLR0913
        self,
        method: str,
        relative: str,
        *,
        phase_started: float,
        phase_limit: int | None = None,
        phase_code: FailureCode | None = None,
        last_known_state: HandoffPhase,
        data: Mapping[str, str] | None = None,
        stream: bool = True,
    ) -> JenkinsResponse:
        budget = RequestBudget(
            operation_started=time.monotonic(),
            phase_started=phase_started,
            phase_limit=phase_limit,
            phase_code=phase_code,
            last_known_state=last_known_state,
        )
        timeout = self.remaining(budget)
        try:
            with self.blocking_deadline(budget):
                response = self.session.request(
                    method,
                    f"{self.settings.base_url}/{relative}",
                    data=data,
                    timeout=(timeout, timeout),
                    allow_redirects=False,
                    stream=stream,
                )
            _ = self.remaining(budget)
            return JenkinsResponse(response=response, budget=budget)
        except requests.Timeout as error:
            _ = self.remaining(budget)
            raise JenkinsRequestError(
                timed_out=True,
                elapsed_seconds=time.monotonic() - budget.operation_started,
            ) from error
        except requests.RequestException as error:
            raise JenkinsRequestError from error

    def parse_status(
        self,
        received: JenkinsResponse,
        name: str,
        phase: HandoffPhase,
    ) -> JsonObject:
        response = received.response
        if response.status_code != HTTP_OK:
            raise JenkinsRequestError
        length = response.headers.get("Content-Length")
        if length is not None and (
            not length.isdecimal() or int(length) > MAX_STATUS_BYTES
        ):
            raise _failure("http_operation", phase)
        content = bytearray()
        try:
            for chunk in self.body_chunks(received):
                content.extend(chunk)
                if len(content) > MAX_STATUS_BYTES:
                    raise _failure("http_operation", phase)
            return cast(
                "JsonObject",
                parse_json(bytes(content), name, max_bytes=MAX_STATUS_BYTES),
            )
        except (InputError, JenkinsRequestError, requests.RequestException) as error:
            raise _failure("http_operation", phase) from error

    def body_chunks(self, received: JenkinsResponse) -> Iterator[bytes]:
        body = cast("_ResponseBody", received.response.raw)
        while True:
            _ = self.remaining(received.budget)
            try:
                with self.blocking_deadline(received.budget):
                    chunk = body.read1(65536, decode_content=True)
            except Exception as error:
                _ = self.remaining(received.budget)
                raise JenkinsRequestError from error
            _ = self.remaining(received.budget)
            if not chunk:
                return
            yield chunk

    def validate_absolute_url(
        self,
        value: object,
        expected_relative: str,
        phase: HandoffPhase,
    ) -> None:
        if not isinstance(value, str):
            raise _failure("identity_mismatch", phase)
        expected = f"{self.settings.base_url}/{expected_relative}"
        if value != expected:
            raise _failure("identity_mismatch", phase)


def _response_failure(
    response: requests.Response,
    phase: HandoffPhase,
) -> HandoffError:
    if response.status_code == HTTP_UNAUTHORIZED:
        return _failure("authentication", phase)
    if response.status_code in {HTTP_FORBIDDEN, HTTP_NOT_FOUND}:
        return _failure("authorization", phase)
    return _failure("http_operation", phase)


def _preflight(client: _JenkinsClient) -> None:
    started = time.monotonic()
    try:
        root = client.request(
            "GET",
            "api/json?tree=mode",
            phase_started=started,
            last_known_state="preflighting",
        )
    except JenkinsRequestError as error:
        raise _failure("reachability", "preflighting") from error
    if root.response.status_code != HTTP_OK:
        raise _response_failure(root.response, "preflighting")
    root.response.close()
    try:
        identity = client.request(
            "GET",
            "whoAmI/api/json?tree=name,authenticated",
            phase_started=started,
            last_known_state="preflighting",
        )
    except JenkinsRequestError as error:
        raise _failure("reachability", "preflighting") from error
    if identity.response.status_code == HTTP_UNAUTHORIZED:
        raise _failure("authentication", "preflighting")
    if identity.response.status_code != HTTP_OK:
        raise _response_failure(identity.response, "preflighting")
    identity_document = client.parse_status(
        identity,
        "Jenkins authenticated identity",
        "preflighting",
    )
    if (
        identity_document.get("authenticated") is not True
        or identity_document.get("name") != client.settings.username
    ):
        raise _failure("authentication", "preflighting")
    try:
        job = client.request(
            "GET",
            f"{client.job_relative}api/json?tree=name,url",
            phase_started=started,
            last_known_state="preflighting",
        )
    except JenkinsRequestError as error:
        raise _failure("reachability", "preflighting") from error
    if job.response.status_code != HTTP_OK:
        raise _response_failure(job.response, "preflighting")
    document = client.parse_status(job, "Jenkins job preflight", "preflighting")
    expected_name = client.settings.job_path.split("/")[-1]
    if document.get("name") != expected_name:
        raise _failure("authorization", "preflighting")
    client.validate_absolute_url(
        document.get("url"),
        client.job_relative,
        "preflighting",
    )


def _submission_parameters(  # noqa: PLR0913
    *,
    execution_id: str,
    requested_ref: str,
    resolved_commit_sha: str,
    run_request_path: str,
    github_run_id: int,
    github_run_attempt: int,
) -> dict[str, str]:
    return {
        "HANDOFF_CONTRACT_VERSION": HANDOFF_CONTRACT_VERSION,
        "EXECUTION_ID": execution_id,
        "REQUESTED_GIT_REF": requested_ref,
        "RESOLVED_COMMIT_SHA": resolved_commit_sha,
        "RUN_REQUEST_PATH": run_request_path,
        "GITHUB_RUN_ID": str(github_run_id),
        "GITHUB_RUN_ATTEMPT": str(github_run_attempt),
    }


def _submit(
    client: _JenkinsClient,
    parameters: Mapping[str, str],
) -> int:
    started = time.monotonic()
    try:
        response = client.request(
            "POST",
            f"{client.job_relative}buildWithParameters",
            phase_started=started,
            phase_limit=client.settings.submission_limit_seconds,
            phase_code="submission_deadline",
            last_known_state="submitting",
            data=parameters,
        )
    except JenkinsRequestError as error:
        elapsed = time.monotonic() - started
        if elapsed >= client.settings.submission_limit_seconds:
            raise _failure(
                "submission_deadline",
                "submitting",
                limit_seconds=client.settings.submission_limit_seconds,
                elapsed_seconds=elapsed,
                last_known_state="submitting",
            ) from error
        raise _failure("indeterminate_submission", "submitting") from error
    raw_response = response.response
    if raw_response.status_code == HTTP_UNAUTHORIZED:
        raise _failure("authentication", "submitting")
    if raw_response.status_code == HTTP_FORBIDDEN:
        raise _failure("authorization", "submitting")
    if raw_response.status_code not in {HTTP_CREATED, HTTP_ACCEPTED}:
        raise _failure("indeterminate_submission", "submitting")
    location = raw_response.headers.get("Location")
    if location is None:
        raise _failure("indeterminate_submission", "submitting")
    absolute = urljoin(f"{client.settings.base_url}/", location)
    parsed_base = urlsplit(client.settings.base_url)
    parsed = urlsplit(absolute)
    base_path = parsed_base.path.rstrip("/")
    match = re.fullmatch(
        rf"{re.escape(base_path)}/queue/item/([1-9][0-9]*)/",
        parsed.path,
    )
    if (
        parsed.scheme != parsed_base.scheme
        or parsed.netloc != parsed_base.netloc
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise _failure("indeterminate_submission", "submitting")
    return int(match.group(1))


def _wait_for_build(client: _JenkinsClient, queue_id: int) -> int:
    started = time.monotonic()
    while True:
        try:
            response = client.request(
                "GET",
                f"queue/item/{queue_id}/api/json?tree=id,cancelled,executable[number,url]",
                phase_started=started,
                phase_limit=client.settings.queue_wait_limit_seconds,
                phase_code="queue_deadline",
                last_known_state="queued",
            )
        except JenkinsRequestError as error:
            raise _failure(
                "http_operation",
                "queued",
                limit_seconds=(
                    client.settings.http_limit_seconds if error.timed_out else None
                ),
                elapsed_seconds=error.elapsed_seconds if error.timed_out else None,
                last_known_state="queued" if error.timed_out else None,
            ) from error
        if response.response.status_code != HTTP_OK:
            if response.response.status_code == HTTP_NOT_FOUND:
                raise _failure("queue_unavailable", "queued")
            raise _response_failure(response.response, "queued")
        document = client.parse_status(response, "Jenkins queue status", "queued")
        if document.get("id") != queue_id:
            raise _failure("identity_mismatch", "queued")
        if document.get("cancelled") is True:
            raise _failure("queue_cancelled", "queued")
        executable = document.get("executable")
        if executable is not None:
            if not isinstance(executable, dict):
                raise _failure("identity_mismatch", "queued")
            executable_values = cast("dict[str, object]", executable)
            number = executable_values.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                raise _failure("identity_mismatch", "queued")
            client.validate_absolute_url(
                executable_values.get("url"),
                f"{client.job_relative}{number}/",
                "queued",
            )
            return number
        remaining = client.remaining(
            RequestBudget(
                operation_started=time.monotonic(),
                phase_started=started,
                phase_limit=client.settings.queue_wait_limit_seconds,
                phase_code="queue_deadline",
                last_known_state="queued",
            )
        )
        time.sleep(min(client.settings.poll_interval_seconds, remaining))


def _build_parameters(document: JsonObject) -> dict[str, str]:
    actions = document.get("actions")
    if not isinstance(actions, list):
        raise _failure("identity_mismatch", "running")
    accepted: dict[str, str] = {}
    for raw_action in cast("list[object]", actions):
        if not isinstance(raw_action, dict):
            continue
        parameters = cast("dict[str, object]", raw_action).get("parameters")
        if parameters is None:
            continue
        if not isinstance(parameters, list):
            raise _failure("identity_mismatch", "running")
        for raw_parameter in cast("list[object]", parameters):
            if not isinstance(raw_parameter, dict):
                raise _failure("identity_mismatch", "running")
            parameter = cast("dict[str, object]", raw_parameter)
            name = parameter.get("name")
            value = parameter.get("value")
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or name in accepted
            ):
                raise _failure("identity_mismatch", "running")
            accepted[name] = value
    return accepted


def _wait_for_completion(
    client: _JenkinsClient,
    queue_id: int,
    build_number: int,
    parameters: Mapping[str, str],
) -> tuple[str, list[object]]:
    started = time.monotonic()
    relative = f"{client.job_relative}{build_number}/"
    while True:
        try:
            response = client.request(
                "GET",
                (
                    f"{relative}api/json?tree=number,queueId,building,result,url,"
                    "actions[parameters[name,value]],artifacts[fileName,relativePath]"
                ),
                phase_started=started,
                phase_limit=client.settings.jenkins_execution_limit_seconds,
                phase_code="build_deadline",
                last_known_state="running",
            )
        except JenkinsRequestError as error:
            raise _failure(
                "http_operation",
                "running",
                limit_seconds=(
                    client.settings.http_limit_seconds if error.timed_out else None
                ),
                elapsed_seconds=error.elapsed_seconds if error.timed_out else None,
                last_known_state="running" if error.timed_out else None,
            ) from error
        if response.response.status_code != HTTP_OK:
            raise _response_failure(response.response, "running")
        document = client.parse_status(response, "Jenkins build status", "running")
        client.validate_absolute_url(document.get("url"), relative, "running")
        if (
            document.get("number") != build_number
            or document.get("queueId") != queue_id
        ):
            raise _failure("identity_mismatch", "running")
        if _build_parameters(document) != dict(parameters):
            raise _failure("identity_mismatch", "running")
        building = document.get("building")
        result = document.get("result")
        if building is True and result is None:
            remaining = client.remaining(
                RequestBudget(
                    operation_started=time.monotonic(),
                    phase_started=started,
                    phase_limit=client.settings.jenkins_execution_limit_seconds,
                    phase_code="build_deadline",
                    last_known_state="running",
                )
            )
            time.sleep(min(client.settings.poll_interval_seconds, remaining))
            continue
        if (
            building is not False
            or not isinstance(result, str)
            or result not in JENKINS_RESULTS
        ):
            raise _failure("http_operation", "running")
        artifacts = document.get("artifacts")
        if not isinstance(artifacts, list):
            raise _failure(
                "missing_artifact",
                "running",
                jenkins_result=result,
            )
        return result, cast("list[object]", artifacts)


def _artifact_inventory(raw_artifacts: list[object]) -> set[str]:
    paths: list[str] = []
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, dict):
            raise _failure("invalid_artifact", "retrieving")
        artifact = cast("dict[str, object]", raw_artifact)
        filename = artifact.get("fileName")
        relative_path = artifact.get("relativePath")
        if (
            not isinstance(filename, str)
            or not isinstance(relative_path, str)
            or Path(relative_path).name != filename
        ):
            raise _failure("invalid_artifact", "retrieving")
        paths.append(relative_path)
    if len(paths) != len(set(paths)):
        raise _failure("invalid_artifact", "retrieving")
    return set(paths)


def _download(
    client: _JenkinsClient,
    relative: str,
    *,
    max_bytes: int,
    retrieval_started: float,
) -> bytes:
    try:
        response = client.request(
            "GET",
            relative,
            phase_started=retrieval_started,
            phase_limit=client.settings.retrieval_limit_seconds,
            phase_code="retrieval_deadline",
            last_known_state="retrieving",
            stream=True,
        )
    except JenkinsRequestError as error:
        raise _failure(
            "http_operation",
            "retrieving",
            limit_seconds=(
                client.settings.http_limit_seconds if error.timed_out else None
            ),
            elapsed_seconds=error.elapsed_seconds if error.timed_out else None,
            last_known_state="retrieving" if error.timed_out else None,
        ) from error
    raw_response = response.response
    if raw_response.status_code == HTTP_NOT_FOUND:
        raise _failure("missing_artifact", "retrieving")
    if raw_response.status_code != HTTP_OK:
        raise _response_failure(raw_response, "retrieving")
    length = raw_response.headers.get("Content-Length")
    if length is not None and (not length.isdecimal() or int(length) > max_bytes):
        raise _failure("invalid_artifact", "retrieving")
    content = bytearray()
    try:
        for chunk in client.body_chunks(response):
            content.extend(chunk)
            if len(content) > max_bytes:
                raise _failure("invalid_artifact", "retrieving")
    except (JenkinsRequestError, requests.RequestException) as error:
        raise _failure("http_operation", "retrieving") from error
    if length is not None and len(content) != int(length):
        raise _failure("invalid_artifact", "retrieving")
    return bytes(content)


def _write_artifact(root: Path, relative_path: str, content: bytes) -> None:
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        _ = stream.write(content)
        stream.flush()
        _ = os.fsync(stream.fileno())


def _quote_artifact_path(relative_path: str) -> str:
    return "/".join(quote(component, safe="") for component in relative_path.split("/"))


def _retrieve_bundle(  # noqa: C901
    client: _JenkinsClient,
    build_number: int,
    raw_artifacts: list[object],
    identified: Mapping[str, object],
    bundle_root: Path,
) -> tuple[PipelineIntegrationResult, JsonObject]:
    retrieval_started = time.monotonic()
    remote_paths = _artifact_inventory(raw_artifacts)
    if PIPELINE_RESULT_PATH not in remote_paths:
        raise _failure("missing_artifact", "retrieving")
    artifact_base = f"{client.job_relative}{build_number}/artifact/"
    result_content = _download(
        client,
        f"{artifact_base}{_quote_artifact_path(PIPELINE_RESULT_PATH)}",
        max_bytes=MAX_RESULT_BYTES,
        retrieval_started=retrieval_started,
    )
    try:
        result = parse_pipeline_result(result_content)
    except (InputError, ValidationError) as error:
        raise _failure("invalid_artifact", "retrieving") from error
    returned_identity: dict[str, object] = {
        "execution_id": result.execution_id,
        "repository": result.repository,
        "requested_ref": result.requested_ref,
        "resolved_commit_sha": result.resolved_commit_sha,
        "scenario_id": result.scenario_id,
        "testcase_id": result.testcase_id,
        "combination_id": result.combination_id,
        "profile_id": result.profile_id,
        "inputs": [item.model_dump(mode="json") for item in result.input_provenance],
    }
    expected_identity = {key: identified[key] for key in returned_identity}
    if returned_identity != expected_identity:
        raise _failure("identity_mismatch", "retrieving")
    expected_paths = {PIPELINE_RESULT_PATH, *(item.path for item in result.artifacts)}
    if remote_paths != expected_paths:
        raise _failure("invalid_artifact", "retrieving")
    if bundle_root.exists() or bundle_root.is_symlink():
        msg = "bundle root must not already exist"
        raise InputError(msg)
    bundle_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{bundle_root.name}.handoff-", dir=bundle_root.parent)
    )
    try:
        _write_artifact(staging, PIPELINE_RESULT_PATH, result_content)
        for artifact in result.artifacts:
            content = _download(
                client,
                f"{artifact_base}{_quote_artifact_path(artifact.path)}",
                max_bytes=artifact.byte_size,
                retrieval_started=retrieval_started,
            )
            if len(content) != artifact.byte_size:
                raise _failure("invalid_artifact", "retrieving")
            _write_artifact(staging, artifact.path, content)
        try:
            validated = validate_bundle(staging)
        except (InputError, ValidationError, OSError) as error:
            raise _failure("invalid_artifact", "retrieving") from error
        if validated.execution_id != identified["execution_id"]:
            raise _failure("identity_mismatch", "retrieving")
        _ = staging.replace(bundle_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    document = cast("JsonObject", result.model_dump(mode="json", exclude_none=True))
    return result, document


def _cancel_correlated(  # noqa: PLR0911
    client: _JenkinsClient,
    queue_id: int | None,
    build_number: int | None,
) -> str | None:
    started = time.monotonic()
    try:
        if build_number is not None:
            relative = f"{client.job_relative}{build_number}/stop"
            observation = f"{client.job_relative}{build_number}/api/json?tree=result"
        elif queue_id is not None:
            relative = f"queue/cancelItem?id={queue_id}"
            observation = f"queue/item/{queue_id}/api/json?tree=cancelled"
        else:
            return None
        _ = client.request(
            "POST",
            relative,
            phase_started=started,
            phase_limit=min(client.settings.http_limit_seconds, 5),
            phase_code="http_operation",
            last_known_state="cancelled",
        )
        observation_limit = min(client.settings.http_limit_seconds, 2)
        while time.monotonic() - started < observation_limit:
            response = client.request(
                "GET",
                observation,
                phase_started=started,
                phase_limit=observation_limit,
                phase_code="http_operation",
                last_known_state="cancelled",
            )
            if response.response.status_code != HTTP_OK:
                return None
            document = client.parse_status(
                response,
                "Jenkins cancellation status",
                "cancelled",
            )
            if build_number is None:
                if document.get("cancelled") is True:
                    return None
            else:
                result = document.get("result")
                if isinstance(result, str) and result in JENKINS_RESULTS:
                    return result
            remaining = observation_limit - (time.monotonic() - started)
            if remaining <= 0:
                return None
            time.sleep(min(client.settings.poll_interval_seconds, remaining))
    except (HandoffError, JenkinsRequestError, requests.RequestException):
        return None
    return None


def handoff_jenkins(  # noqa: C901, PLR0913, PLR0915
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    execution_id: str,
    github_run_id: str,
    github_run_attempt: str,
    bundle_root: Path,
    receipt_path: Path,
    environment: Mapping[str, str],
    progress: Callable[[str], None],
) -> JsonObject:
    """Submit, correlate, retrieve, and validate one exact Jenkins build."""
    if (
        POSITIVE_DECIMAL.fullmatch(github_run_id) is None
        or POSITIVE_DECIMAL.fullmatch(github_run_attempt) is None
    ):
        msg = "GitHub run ID and attempt must be positive decimal integers"
        raise InputError(msg)
    writer = _ReceiptWriter(
        receipt_path,
        execution_id=execution_id,
        github_run_id=int(github_run_id),
        github_run_attempt=int(github_run_attempt),
        progress=progress,
    )
    outer_started = time.monotonic()
    client: _JenkinsClient | None = None
    queue_id: int | None = None
    build_number: int | None = None
    try:
        try:
            identified = identify_committed_run(
                repository_path=repository_path,
                requested_ref=requested_ref,
                resolved_commit=resolved_commit,
                run_request_path=run_request_path,
                execution_id=execution_id,
            )
            if bundle_root.exists() or bundle_root.is_symlink():
                msg = "bundle root must not already exist"
                raise InputError(msg)
        except (InputError, OSError, ValidationError):
            writer.fail(_failure("input_validation", "validating"))
            raise
        try:
            settings = load_handoff_settings(environment)
            if settings.repository != identified["repository"]:
                msg = "trusted repository identity does not match the committed run"
                raise InputError(msg)
        except InputError:
            writer.fail(_failure("configuration", "validating"))
            raise
        client = _JenkinsClient(settings, outer_started)
        writer.transition("preflighting")
        _preflight(client)
        writer.transition("submitting")
        parameters = _submission_parameters(
            execution_id=execution_id,
            requested_ref=requested_ref,
            resolved_commit_sha=cast("str", identified["resolved_commit_sha"]),
            run_request_path=run_request_path,
            github_run_id=int(github_run_id),
            github_run_attempt=int(github_run_attempt),
        )
        queue_id = _submit(client, parameters)
        writer.transition("queued", queue_id=queue_id)
        build_number = _wait_for_build(client, queue_id)
        writer.transition(
            "running",
            build_number=build_number,
            job_relative=client.job_relative,
        )
        jenkins_result, raw_artifacts = _wait_for_completion(
            client,
            queue_id,
            build_number,
            parameters,
        )
        if jenkins_result in {"ABORTED", "NOT_BUILT"}:
            raise _failure(
                "jenkins_aborted",
                "running",
                jenkins_result=jenkins_result,
            )
        if jenkins_result == "UNSTABLE":
            raise _failure(
                "jenkins_unstable",
                "running",
                jenkins_result=jenkins_result,
            )
        writer.transition("retrieving")
        _result, document = _retrieve_bundle(
            client,
            build_number,
            raw_artifacts,
            identified,
            bundle_root,
        )
        if jenkins_result == "FAILURE":
            raise _failure(
                "jenkins_failure",
                "retrieving",
                jenkins_result=jenkins_result,
                result=document,
            )
        writer.succeed()
        return document
    except ExternalCancellation:
        observed = (
            _cancel_correlated(client, queue_id, build_number)
            if client is not None
            else None
        )
        writer.cancel(observed)
        raise
    except HandoffError as error:
        writer.fail(error)
        raise
    finally:
        if client is not None:
            client.close()

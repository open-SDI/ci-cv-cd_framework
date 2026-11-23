"""Black-box checks for the containerized GitHub handoff runner."""

from __future__ import annotations

import ast
import json
import os
import pty
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from collections.abc import Generator

REPOSITORY_ROOT = Path(__file__).parents[2]
JENKINS_ROOT = REPOSITORY_ROOT / "deployment" / "jenkins"
RUNNER_ROOT = JENKINS_ROOT / "runner"
STACK = JENKINS_ROOT / "bin" / "stack"
RUNNERS = ("runner-upstream", "runner-fork")
PROXY_UP_ARGS = (
    "--no-build --no-deps --detach --wait --wait-timeout 240 runner-fork-proxy"
)
TOKEN = "AREGISTRATIONTOKEN123"  # noqa: S105 - fake value for the fake runner.
FAKE_DOCKER = r"""#!/bin/sh
printf '%s|profiles=%s\n' "$*" "${COMPOSE_PROFILES-unset}" >> "$EVENT_LOG"
listed() { case " $1 " in *" $2 "*) return 0 ;; esac; return 1; }
case "$*" in
  'version --format {{.Server.Os}}/{{.Server.Arch}}') printf 'linux/amd64\n' ;;
  'image inspect --format {{json .Config.Volumes}}'*) printf 'null\n' ;;
  *' -T runner-'*' show')
    service=${*##* -T }; service=${service%% *}
    listed "${FAKE_REGISTERED-}" "$service" || exit 3
    printf 'https://github.com/example/repository %s\n' "$service" ;;
  *' -T runner-'*' check') [ -z "${FAKE_UNREACHABLE-}" ] ;;
  *' -T runner-'*' register '*)
    [ "$(cat)" = "$EXPECTED_TOKEN" ] || exit 9
    printf 'registered\n' ;;
  *' ps --status running --quiet runner-'*)
    service=${*##* }
    ! listed "${FAKE_RUNNING-}" "$service" || printf 'id-%s\n' "$service" ;;
  'ps --quiet --filter label=com.docker.compose.project='*)
    service=${*##*com.docker.compose.service=}; service=${service%% *}
    ! listed "${FAKE_RUNNING-}" "$service" || printf 'id-%s\n' "$service" ;;
  'exec id-'*' sdi-runner-probe worker')
    listed "${FAKE_BUSY-}" "${2#id-}" ;;
esac
"""
FAKE_UV = r"""#!/bin/sh
case "$*" in
  *adapter-image*) printf 'docker.io/example/agent@sha256:%064d\n' 1 2 3 4 ;;
  *sdi-integration*validate-installation*) exit 0 ;;
  *) exit 2 ;;
esac
"""
SECRET_NAMES = (
    "admin",
    "handoff",
    "openai",
    "integration",
    "ci",
    "image-build",
    "cv",
    "cd",
)


def _compose() -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        YAML(typ="safe").load(  # pyright: ignore[reportUnknownMemberType]
            (JENKINS_ROOT / "compose.yaml").read_text(encoding="utf-8")
        ),
    )


@pytest.mark.parametrize("name", RUNNERS)
def test_runner_service_is_hardened_and_reaches_only_the_controller_and_its_proxy(
    name: str,
) -> None:
    compose = _compose()
    runner = compose["services"][name]

    assert runner["profiles"] == [name]
    assert runner["image"] == "sdi-jenkins-runner:2.337.0"
    assert runner["build"] == {
        "context": "../..",
        "dockerfile": "deployment/jenkins/runner/Dockerfile",
    }
    assert runner["platform"] == "linux/amd64"
    assert runner["restart"] == "no"
    assert runner["init"] is True
    assert runner["read_only"] is True
    assert runner["cap_drop"] == ["ALL"]
    assert runner["security_opt"] == ["no-new-privileges:true"]
    proxy_url = f"http://{name}-proxy:3128"
    assert runner["environment"] == {
        "https_proxy": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "HTTP_PROXY": proxy_url,
        "no_proxy": "controller,localhost,127.0.0.1",
        "NO_PROXY": "controller,localhost,127.0.0.1",
    }
    assert "env_file" not in runner
    assert "secrets" not in runner
    assert "ports" not in runner
    assert "docker.sock" not in json.dumps(runner)
    assert runner["volumes"] == [f"{name}-state:/var/lib/sdi-runner"]
    assert {entry.split(":", maxsplit=1)[0] for entry in runner["tmpfs"]} == {
        "/home/runner",
        "/tmp",  # noqa: S108 - container mount point, not a host temp file.
    }
    assert all("size=" in entry for entry in runner["tmpfs"])
    assert runner["depends_on"] == {
        "controller": {"condition": "service_healthy"},
        f"{name}-proxy": {"condition": "service_healthy"},
    }
    assert runner["healthcheck"]["test"] == ["CMD", "sdi-runner-probe", "listener"]

    controller_network = f"{name}-controller"
    egress_network = f"{name}-egress"
    assert runner["networks"] == [controller_network]
    assert compose["networks"][controller_network] == {"internal": True}
    assert compose["networks"][egress_network] is None
    attached = {
        network: {
            service
            for service, definition in compose["services"].items()
            if network in definition.get("networks", [])
        }
        for network in (controller_network, egress_network)
    }
    assert attached == {
        controller_network: {"controller", name, f"{name}-proxy"},
        egress_network: {f"{name}-proxy"},
    }
    assert f"{name}-state" in compose["volumes"]


@pytest.mark.parametrize("name", RUNNERS)
def test_runner_proxy_is_a_hardened_stateless_sidecar_of_its_runner(
    name: str,
) -> None:
    compose = _compose()
    proxy = compose["services"][f"{name}-proxy"]

    assert proxy["profiles"] == [name]
    assert proxy["image"] == "sdi-jenkins-runner-proxy:6"
    assert proxy["build"] == {"context": "runner-proxy", "dockerfile": "Dockerfile"}
    assert proxy["platform"] == "linux/amd64"
    assert proxy["restart"] == "no"
    assert proxy["init"] is True
    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]
    assert proxy["security_opt"] == ["no-new-privileges:true"]
    assert proxy["networks"] == [f"{name}-controller", f"{name}-egress"]
    for absent in ("volumes", "ports", "secrets", "environment", "env_file"):
        assert absent not in proxy
    assert "depends_on" not in proxy
    assert [entry.split(":", maxsplit=1)[0] for entry in proxy["tmpfs"]] == [
        "/run/squid"
    ]
    assert proxy["healthcheck"]["test"][0] == "CMD"

    dockerfile = (JENKINS_ROOT / "runner-proxy" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "FROM ${RUNTIME_IMAGE}\nLABEL org.sdi.project=ci-cv-cd_framework\n" in (
        dockerfile
    )
    assert dockerfile.rstrip().splitlines()[-2:] == [
        "USER proxy",
        'ENTRYPOINT ["squid", "-N", "-f", "/etc/squid/squid.conf"]',
    ]


def test_runner_proxy_admits_only_https_tunnels_to_github_actions_hosts() -> None:
    lines = [
        line.split()
        for line in (JENKINS_ROOT / "runner-proxy" / "squid.conf")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    allowlist = next(line for line in lines if line[:2] == ["acl", "allowlisted"])
    access = [" ".join(line[1:]) for line in lines if line[0] == "http_access"]

    assert allowlist[2:4] == ["dstdomain", "-n"]
    assert set(allowlist[4:]) == {
        "github.com",
        "api.github.com",
        "codeload.github.com",
        ".actions.githubusercontent.com",
        ".blob.core.windows.net",
    }
    assert access.index("allow CONNECT allowlisted") > access.index(
        "deny CONNECT ip_literal"
    )
    assert access[-1] == "deny all"
    assert not any(line[0] == "cache_dir" for line in lines)


def test_runner_egress_probes_and_proxy_allowlist_cover_each_other() -> None:
    entrypoint = (RUNNER_ROOT / "sdi-runner-entrypoint").read_text(encoding="utf-8")
    probed = ast.literal_eval(
        entrypoint.split("ALLOWLISTED = ", 1)[1].split("\n)\n", 1)[0] + "\n)"
    )
    squid = (JENKINS_ROOT / "runner-proxy" / "squid.conf").read_text(encoding="utf-8")
    allowlist = next(
        line.split()[4:]
        for line in squid.splitlines()
        if line.startswith("acl allowlisted dstdomain -n ")
    )

    def admits(entry: str, host: str) -> bool:
        return host.endswith(entry) if entry.startswith(".") else host == entry

    assert probed
    assert all(any(admits(entry, host) for entry in allowlist) for host in probed)
    assert all(any(admits(entry, host) for host in probed) for entry in allowlist)


def test_runner_registers_once_without_updates_or_default_labels() -> None:
    entrypoint = (RUNNER_ROOT / "sdi-runner-entrypoint").read_text(encoding="utf-8")

    assert "--disableupdate" in entrypoint
    assert "--no-default-labels" in entrypoint
    assert "--labels sdi-jenkins-handoff \\" in entrypoint
    assert "--runnergroup" not in entrypoint
    assert "ACTIONS_RUNNER_INPUT_TOKEN" in entrypoint
    assert "--token" not in entrypoint
    assert "[ ! -t 0 ]" in entrypoint
    assert "RUNNER_MANUALLY_TRAP_SIG=1" in entrypoint
    assert "exec ./run.sh" in entrypoint


def test_runner_image_verifies_the_pinned_download_and_runs_unprivileged() -> None:
    dockerfile = (RUNNER_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ADD --checksum=sha256:${RUNNER_SHA256} \\" in dockerfile
    assert (
        "https://github.com/actions/runner/releases/download/"
        "v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
    ) in dockerfile
    assert (
        'test "$(/opt/actions-runner/bin/Runner.Listener --version)"'
        ' = "$RUNNER_VERSION"'
    ) in dockerfile
    assert "FROM runtime\nLABEL org.sdi.project=ci-cv-cd_framework\n" in dockerfile
    assert dockerfile.rstrip().splitlines()[-2:] == [
        "USER runner",
        'ENTRYPOINT ["/usr/local/bin/sdi-runner-entrypoint"]',
    ]
    assert "sudo" not in dockerfile


def test_runner_image_and_check_require_the_git_options_the_cli_uses() -> None:
    dockerfile = (RUNNER_ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (RUNNER_ROOT / "sdi-runner-entrypoint").read_text(encoding="utf-8")
    git_input = (
        REPOSITORY_ROOT / "integration/src/sdi_pipeline_integration/_git_input.py"
    ).read_text(encoding="utf-8")

    assert '"--no-lazy-fetch",' in git_input
    assert "    && git --no-lazy-fetch --version\n" in dockerfile
    assert "        git --no-lazy-fetch --version >/dev/null || fail" in entrypoint


def test_runner_network_check_rejects_an_unknown_mode() -> None:
    entrypoint = (RUNNER_ROOT / "sdi-runner-entrypoint").read_text(encoding="utf-8")
    functions = "\n".join(
        entrypoint[entrypoint.index(f"\n{name}() {{\n") :].split("\n}\n", 1)[0] + "\n}"
        for name in ("fail", "network")
    )

    completed = subprocess.run(
        ["sh", "-c", f'{functions}\nnetwork "$1"', "sh", "probe"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HTTPS_PROXY": "http://proxy:3128"},
    )

    assert completed.returncode == 2
    assert "unknown network mode: probe" in completed.stderr


class _FakeJenkins(BaseHTTPRequestHandler):
    """Answer the administrator quiesce and resume calls of `smoke-check`."""

    busy = False
    event_log: Path

    def do_GET(self) -> None:
        if self.path == "/crumbIssuer/api/json":
            body = b'{"crumbRequestField":"Jenkins-Crumb","crumb":"test"}'
        elif self.path.startswith("/queue/api/json"):
            body = b'{"items":[{"id":42}]}' if self.busy else b'{"items":[]}'
        elif self.path.startswith("/job/"):
            body = b'{"builds":[]}'
        elif self.path.startswith("/api/json"):
            body = b'{"quietingDown":false}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        with self.event_log.open("a", encoding="utf-8") as log:
            log.write(f"jenkins {self.path}\n")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *arguments: object) -> None:  # noqa: A002
        del format, arguments


@contextmanager
def _jenkins(events: Path, *, busy: bool) -> Generator[int]:
    handler = type("Handler", (_FakeJenkins,), {"busy": busy, "event_log": events})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class _Stack:
    """One installation driven through `bin/stack` against a fake Docker."""

    def __init__(self, root: Path, *, runners: str, port: int = 8080) -> None:
        self.events = root / "events.log"
        self.events.touch()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        for name, script in (("docker", FAKE_DOCKER), ("uv", FAKE_UV)):
            (fake_bin / name).write_text(script, encoding="utf-8")
            (fake_bin / name).chmod(0o755)
        secrets = root / "secrets"
        secrets.mkdir(mode=0o700)
        secret_files: dict[str, Path] = {}
        for index, name in enumerate(SECRET_NAMES, start=1):
            secret_files[name] = secrets / name
            secret_files[name].write_text(f"{index:x}" * 64, encoding="utf-8")
            secret_files[name].chmod(0o600)
        self.config = root / "controller.env"
        self.config.write_text(
            f"JENKINS_HTTP_PORT={port}\n"
            "JENKINS_ADMIN_ID=administrator\n"
            "JENKINS_HANDOFF_ID=github-handoff\n"
            "JENKINS_REPOSITORY_URL=https://github.com/example/repository.git\n"
            "JENKINS_FORK_REPOSITORY_URL="
            "https://github.com/example-fork/repository.git\n"
            "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080\n"
            f"JENKINS_ADMIN_PASSWORD_FILE={secret_files['admin']}\n"
            f"JENKINS_HANDOFF_PASSWORD_FILE={secret_files['handoff']}\n"
            f"JENKINS_OPENAI_API_KEY_FILE={secret_files['openai']}\n"
            f"JENKINS_INTEGRATION_AGENT_SECRET_FILE={secret_files['integration']}\n"
            f"JENKINS_CI_AGENT_SECRET_FILE={secret_files['ci']}\n"
            f"JENKINS_IMAGE_BUILD_AGENT_SECRET_FILE={secret_files['image-build']}\n"
            f"JENKINS_CV_AGENT_SECRET_FILE={secret_files['cv']}\n"
            f"JENKINS_CD_AGENT_SECRET_FILE={secret_files['cd']}\n"
            f"GITHUB_RUNNERS={runners}\n",
            encoding="utf-8",
        )
        self.environment = {
            "EVENT_LOG": str(self.events),
            "EXPECTED_TOKEN": TOKEN,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        }

    def run(
        self, *arguments: str, stdin: str | int = "", **state: str
    ) -> subprocess.CompletedProcess[str]:
        """Run one stack action with the fake Docker in the given state."""
        return subprocess.run(
            [STACK, "--config", self.config, *arguments],
            check=False,
            capture_output=True,
            env=self.environment | state,
            stdin=stdin if isinstance(stdin, int) else None,
            input=stdin if isinstance(stdin, str) else None,
            text=True,
        )

    def log(self) -> list[str]:
        """Docker and Jenkins calls in order, without the profile suffix."""
        return [
            line.split("|", maxsplit=1)[0]
            for line in self.events.read_text(encoding="utf-8").splitlines()
        ]

    def profiles(self, call: str) -> list[str]:
        """Return the Compose profiles of each Docker call ending with `call`."""
        return [
            line.rsplit("|profiles=", maxsplit=1)[1]
            for line in self.events.read_text(encoding="utf-8").splitlines()
            if line.split("|", maxsplit=1)[0].endswith(call)
        ]


@pytest.mark.parametrize(
    ("options", "name", "replace"),
    [
        ((), "jenkins-handoff-stack-fork", ""),
        (("--name", "etri-handoff"), "etri-handoff", ""),
        (("--replace",), "jenkins-handoff-stack-fork", " --replace"),
    ],
)
def test_register_runner_pipes_the_token_to_a_one_off_runner_container(
    tmp_path: Path, options: tuple[str, ...], name: str, replace: str
) -> None:
    stack = _Stack(tmp_path, runners="fork")

    completed = stack.run(
        "register-runner",
        "fork",
        *options,
        stdin=f"{TOKEN}\n",
        FAKE_REGISTERED="runner-fork" if replace else "",
    )

    assert completed.returncode == 0, completed.stderr
    assert "registered" in completed.stdout
    log = stack.log()
    registration = (
        "run --rm --no-deps -T runner-fork register "
        f"https://github.com/example-fork/repository {name}{replace}"
    )
    registrations = [line for line in log if line.endswith(registration)]
    assert len(registrations) == 1
    assert stack.profiles(registration) == ["runner-fork"]
    assert stack.profiles("build runner-fork runner-fork-proxy") == ["runner-fork"]
    proxy = next(line for line in log if line.endswith(" up " + PROXY_UP_ARGS))
    check = next(line for line in log if line.endswith("-T runner-fork check"))
    assert log.index(proxy) < log.index(check) < log.index(registrations[0])
    assert TOKEN not in stack.events.read_text(encoding="utf-8")
    assert TOKEN not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("runners", "state", "message"),
    [
        ("upstream", {}, "add fork to GITHUB_RUNNERS"),
        ("fork", {"FAKE_RUNNING": "runner-fork"}, "stop the fork runner"),
        ("fork", {"FAKE_REGISTERED": "runner-fork"}, "pass --replace"),
        ("fork", {"FAKE_UNREACHABLE": "yes"}, "the fork runner failed its network"),
    ],
)
def test_register_runner_refuses_unsafe_or_redundant_registration(
    tmp_path: Path, runners: str, state: dict[str, str], message: str
) -> None:
    stack = _Stack(tmp_path, runners=runners)

    completed = stack.run("register-runner", "fork", stdin=f"{TOKEN}\n", **state)

    assert completed.returncode == 2
    assert message in completed.stderr
    assert not any(" register " in line for line in stack.log())


def test_register_runner_refuses_a_token_typed_at_a_terminal(tmp_path: Path) -> None:
    stack = _Stack(tmp_path, runners="fork")
    controller, terminal = pty.openpty()
    try:
        completed = stack.run("register-runner", "fork", stdin=terminal)
    finally:
        os.close(controller)
        os.close(terminal)

    assert completed.returncode == 2
    assert "pipe the registration token on standard input" in completed.stderr
    assert not any("compose" in line for line in stack.log())


def test_start_refuses_an_unregistered_runner_with_its_registration_command(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path, runners="upstream,fork")

    completed = stack.run("start", FAKE_REGISTERED="runner-upstream")

    assert completed.returncode == 2
    assert (
        "the fork runner is not registered; run: gh api -X POST "
        "repos/example-fork/repository/actions/runners/registration-token "
        "--jq .token | deployment/jenkins/bin/stack register-runner fork"
    ) in completed.stderr
    assert not any(" up " in line for line in stack.log())


def test_start_refuses_while_a_runner_executes_a_job(tmp_path: Path) -> None:
    stack = _Stack(tmp_path, runners="fork")

    completed = stack.run(
        "start",
        FAKE_REGISTERED="runner-fork",
        FAKE_RUNNING="runner-fork",
        FAKE_BUSY="runner-fork",
    )

    assert completed.returncode == 2
    assert "executing a handoff job" in completed.stderr
    full_up = " up --no-build --detach --wait --wait-timeout 240"
    assert not any(line.endswith(full_up) for line in stack.log())


@pytest.mark.parametrize(
    ("runners", "services", "profiles"),
    [
        ("", "", ""),
        ("fork", " runner-fork runner-fork-proxy", "runner-fork"),
        (
            "fork,upstream",
            " runner-upstream runner-fork runner-upstream-proxy runner-fork-proxy",
            "runner-upstream,runner-fork",
        ),
    ],
)
def test_start_builds_and_starts_only_the_configured_runners(
    tmp_path: Path, runners: str, services: str, profiles: str
) -> None:
    stack = _Stack(tmp_path, runners=runners)

    completed = stack.run("start", FAKE_REGISTERED="runner-upstream runner-fork")

    assert completed.returncode == 0, completed.stderr
    assert stack.profiles(f"build integration ci image-build cv cd{services}") == [
        profiles
    ]
    assert stack.profiles("up --no-build --detach --wait --wait-timeout 240") == [
        profiles
    ]
    assert stack.profiles(
        "pull --quiet --policy missing buildkitd registry cv-engine"
    ) == [profiles]
    log = stack.log()
    pull = next(i for i, line in enumerate(log) if "pull --quiet" in line)
    build = next(i for i, line in enumerate(log) if "build integration" in line)
    assert pull < build
    proxy_check = f"build --check --file {JENKINS_ROOT}/runner-proxy/Dockerfile"
    assert any(line.startswith(proxy_check) for line in log)
    agent_check = (
        f"build --check --target image-build --file {JENKINS_ROOT}/agents/Dockerfile"
    )
    assert any(line.startswith(agent_check) for line in log)


@pytest.mark.parametrize("action", ["stop", "status", "logs", "destroy"])
def test_configuration_free_actions_include_every_runner(
    tmp_path: Path, action: str
) -> None:
    stack = _Stack(tmp_path, runners="")

    completed = stack.run(action)

    assert completed.returncode == 0, completed.stderr
    compose_calls = [
        line
        for line in stack.events.read_text(encoding="utf-8").splitlines()
        if line.startswith("compose ")
    ]
    assert compose_calls
    assert all(line.endswith("|profiles=*") for line in compose_calls)


def test_github_runners_rejects_unknown_repositories(tmp_path: Path) -> None:
    stack = _Stack(tmp_path, runners="upstream,elsewhere")

    completed = stack.run("validate")

    assert completed.returncode == 2
    assert "GITHUB_RUNNERS must be empty or list upstream, fork, or both" in (
        completed.stderr
    )


def test_quiesce_refuses_while_a_runner_executes_a_job(tmp_path: Path) -> None:
    with _jenkins(tmp_path / "events.log", busy=False) as port:
        stack = _Stack(tmp_path, runners="fork", port=port)
        completed = stack.run(
            "quiesce", FAKE_RUNNING="runner-fork", FAKE_BUSY="runner-fork"
        )

    assert completed.returncode == 2
    assert "executing a handoff job" in completed.stderr
    assert not any(line.startswith(("stop ", "jenkins ")) for line in stack.log())


def test_quiesce_stops_runners_before_jenkins_and_resume_restarts_them(
    tmp_path: Path,
) -> None:
    with _jenkins(tmp_path / "events.log", busy=False) as port:
        stack = _Stack(tmp_path, runners="fork", port=port)
        quiesced = stack.run("quiesce", FAKE_RUNNING="runner-fork")
        resumed = stack.run("resume")

    assert quiesced.returncode == 0, quiesced.stderr
    assert resumed.returncode == 0, resumed.stderr
    log = stack.log()
    assert log.index("stop --time 60 id-runner-fork") < log.index("jenkins /quietDown")
    assert not any(line.startswith("start ") for line in log)
    assert log.index("jenkins /cancelQuietDown") < len(log) - 1
    assert log[-1].endswith(
        "up --no-build --no-deps --detach --wait --wait-timeout 240 "
        "runner-fork-proxy runner-fork"
    )


def test_quiesce_restarts_the_runners_when_jenkins_has_work(tmp_path: Path) -> None:
    with _jenkins(tmp_path / "events.log", busy=True) as port:
        stack = _Stack(tmp_path, runners="fork", port=port)
        completed = stack.run("quiesce", FAKE_RUNNING="runner-fork")

    assert completed.returncode == 1
    log = stack.log()
    assert log.index("stop --time 60 id-runner-fork") < log.index("jenkins /quietDown")
    assert log[-1] == "start id-runner-fork"

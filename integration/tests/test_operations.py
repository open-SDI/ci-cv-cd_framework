"""Black-box checks for scaffold operation and recovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
INSTALLATION = REPOSITORY_ROOT / "deployment" / "installation.env"
RECOVERY = REPOSITORY_ROOT / "deployment" / "jenkins" / "bin" / "recovery"
RUNNER = REPOSITORY_ROOT / "deployment" / "github-runner" / "bin" / "runner"
SMOKE_CHECK = REPOSITORY_ROOT / "deployment" / "jenkins" / "bin" / "smoke-check"
TOKEN_REMOVER = (
    REPOSITORY_ROOT
    / "deployment"
    / "jenkins"
    / "libexec"
    / "RemoveArchivedApiTokens.java"
)
CONTROLLER_IMAGE = next(
    line.partition("=")[2]
    for line in INSTALLATION.read_text(encoding="utf-8").splitlines()
    if line.startswith("SDI_JENKINS_CONTROLLER_IMAGE=")
)


def _run_recovery(
    fake_bin: Path, log: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [RECOVERY, *arguments],
        check=False,
        capture_output=True,
        env=os.environ
        | {
            "OPERATIONS_LOG": str(log),
            "FAKE_VOLUME_MOUNTPOINT": str(fake_bin.parent / "controller-volume"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SDI_RECOVERY_STACK_COMMAND": str(fake_bin / "stack"),
            "SDI_RECOVERY_SMOKE_CHECK_COMMAND": str(fake_bin / "smoke-check"),
        },
        text=True,
    )


def _run_token_remover(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--volume",
            f"{root}:/target",
            "--volume",
            f"{TOKEN_REMOVER}:/RemoveArchivedApiTokens.java:ro",
            CONTROLLER_IMAGE,
            "java",
            "/RemoveArchivedApiTokens.java",
            "/target/users",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_fake_recovery_tools(fake_bin: Path) -> None:
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'docker %s\\n\' "$*" >> "$OPERATIONS_LOG"\n'
        'case "$*" in\n'
        "  'volume inspect --format {{.Mountpoint}} '*) "
        "printf '%s\\n' \"$FAKE_VOLUME_MOUNTPOINT\" ;;\n"
        "  *'volume inspect recovery-test-sdi-recovery-credentials-'*) exit 1 ;;\n"
        "  'volume inspect '*) exit 0 ;;\n"
        "  *'find /target -mindepth 1 -print -quit'*) exit 0 ;;\n"
        "  *'tar --create'*) printf 'complete-controller-volume' ;;\n"
        "  *'cat /source/secrets/jenkins.slaves.JnlpSlaveAgentProtocol.secret'*) "
        "printf 'encrypted-fresh-agent-key' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    stack = fake_bin / "stack"
    stack.write_text(
        '#!/bin/sh\nprintf \'stack %s\\n\' "$*" >> "$OPERATIONS_LOG"\n',
        encoding="utf-8",
    )
    stack.chmod(0o755)

    smoke_check = fake_bin / "smoke-check"
    smoke_check.write_text(
        "#!/bin/sh\n"
        'printf \'smoke-check %s\\n\' "$*" >> "$OPERATIONS_LOG"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --agent-secret-directory ]; then directory=$2; break; fi\n'
        "  shift\n"
        "done\n"
        "index=1\n"
        "for name in integration ci image-build cv cd; do\n"
        "  case $index in 1) digit=a ;; 2) digit=b ;; 3) digit=c ;; "
        "4) digit=d ;; 5) digit=e ;; esac\n"
        "  value=\n"
        "  while [ ${#value} -lt 64 ]; do value=$value$digit; done\n"
        '  printf \'%s\\n\' "$value" > "$directory/$name-agent-secret"\n'
        '  chmod 600 "$directory/$name-agent-secret"\n'
        "  index=$((index + 1))\n"
        "done\n",
        encoding="utf-8",
    )
    smoke_check.chmod(0o755)

    commit = subprocess.run(
        ["git", "-C", REPOSITORY_ROOT, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git = fake_bin / "git"
    git.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *'rev-parse HEAD') printf '%s\\n' '{commit}' ;;\n"
        "  *'status --porcelain=v1 --untracked-files=all') exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    git.chmod(0o755)


def _write_fresh_credential_config(tmp_path: Path, manifest: Path) -> Path:
    created_at = datetime.fromisoformat(json.loads(manifest.read_bytes())["created_at"])
    values = {
        "JENKINS_ADMIN_PASSWORD_FILE": "fresh-admin-password",
        "JENKINS_HANDOFF_PASSWORD_FILE": "fresh-handoff-password",
        "JENKINS_INTEGRATION_AGENT_SECRET_FILE": "1" * 64,
        "JENKINS_CI_AGENT_SECRET_FILE": "2" * 64,
        "JENKINS_IMAGE_BUILD_AGENT_SECRET_FILE": "3" * 64,
        "JENKINS_CV_AGENT_SECRET_FILE": "4" * 64,
        "JENKINS_CD_AGENT_SECRET_FILE": "5" * 64,
    }
    lines: list[str] = []
    for key, value in values.items():
        path = tmp_path / key.casefold()
        if key in {"JENKINS_ADMIN_PASSWORD_FILE", "JENKINS_HANDOFF_PASSWORD_FILE"}:
            path.write_text(f"{value}\n", encoding="utf-8")
            timestamp = created_at.timestamp() + 1
            os.utime(path, (timestamp, timestamp))
        lines.append(f"{key}={path}")
    config = tmp_path / "controller.env"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def _assert_restore_credential_rotation_order(operations: list[str]) -> None:
    staging_restore = next(
        index for index, line in enumerate(operations) if "tar --extract" in line
    )
    key_removal = next(
        index
        for index, line in enumerate(operations)
        if "rm -f -- /target/secrets/jenkins.slaves" in line
    )
    token_removals = [
        (index, line)
        for index, line in enumerate(operations)
        if "RemoveArchivedApiTokens.java" in line
    ]
    assert len(token_removals) == 2
    assert all(
        "java /RemoveArchivedApiTokens.java" in line for _, line in token_removals
    )
    assert all(line.endswith("/target/users") for _, line in token_removals)
    staging_token_removal = next(
        index for index, line in token_removals if "sdi-recovery-credentials" in line
    )
    target_token_removal = next(
        index
        for index, line in token_removals
        if "recovery-test_jenkins-home:/target" in line
    )
    staging_start = next(
        index
        for index, line in enumerate(operations)
        if "sdi-recovery-credentials" in line and line.endswith(" start-controller")
    )
    provision = next(
        index for index, line in enumerate(operations) if line.startswith("smoke-check")
    )
    staging_verify = next(
        index
        for index, line in enumerate(operations)
        if "sdi-recovery-credentials" in line and "verify-live" in line
    )
    target_restore = max(
        index for index, line in enumerate(operations) if "tar --extract" in line
    )
    key_install = next(
        index
        for index, line in enumerate(operations)
        if "cat > /target/secrets/jenkins.slaves" in line
    )
    target_start = next(
        index
        for index, line in enumerate(operations)
        if "stack --config" in line
        and "sdi-recovery-credentials" not in line
        and line.endswith(" start")
    )
    assert staging_restore < token_removals[0][0]
    assert staging_restore < staging_token_removal < key_removal < staging_start
    assert staging_start < provision < staging_verify
    assert staging_verify < target_restore < target_token_removal < key_install
    assert key_install < target_start


def test_archived_api_tokens_are_removed_with_user_config_preserved(
    tmp_path: Path,
) -> None:
    users = tmp_path / "users"
    user = users / "archived-user"
    user.mkdir(parents=True)
    config = user / "config.xml"
    config.write_text(
        "<?xml version='1.1' encoding='UTF-8'?>\n"
        "<user><properties>"
        "<jenkins.security.ApiTokenProperty><tokenStore>secret</tokenStore>"
        "</jenkins.security.ApiTokenProperty>"
        "<example.Property><value>preserved</value></example.Property>"
        "</properties></user>\n",
        encoding="utf-8",
    )
    config.chmod(0o640)
    original_stat = config.stat()
    stats = user / "apiTokenStats.xml"
    stats.write_text("<apiTokenStats/>\n", encoding="utf-8")

    completed = _run_token_remover(tmp_path)

    assert completed.returncode == 0, completed.stderr
    serialized = config.read_text(encoding="utf-8")
    assert "jenkins.security.ApiTokenProperty" not in serialized
    assert "<value>preserved</value>" in serialized
    assert config.stat().st_mode & 0o777 == 0o640
    assert config.stat().st_uid == original_stat.st_uid
    assert config.stat().st_gid == original_stat.st_gid
    assert not stats.exists()


def test_archived_api_token_removal_fails_closed_for_invalid_xml(
    tmp_path: Path,
) -> None:
    users = tmp_path / "users"
    user = users / "archived-user"
    user.mkdir(parents=True)
    config = user / "config.xml"
    invalid = "<user><jenkins.security.ApiTokenProperty></user>\n"
    config.write_text(invalid, encoding="utf-8")

    completed = _run_token_remover(tmp_path)

    assert completed.returncode == 1
    assert completed.stderr.strip() == "archived API token removal failed"
    assert config.read_text(encoding="utf-8") == invalid


def test_archived_api_token_removal_rejects_a_users_symlink(tmp_path: Path) -> None:
    archived_users = tmp_path / "archived-users"
    user = archived_users / "archived-user"
    user.mkdir(parents=True)
    config = user / "config.xml"
    config.write_text(
        "<user><properties><jenkins.security.ApiTokenProperty/></properties></user>\n",
        encoding="utf-8",
    )
    (tmp_path / "users").symlink_to(archived_users.name, target_is_directory=True)

    completed = _run_token_remover(tmp_path)

    assert completed.returncode == 1
    assert completed.stderr.strip() == "archived API token removal failed"
    assert "jenkins.security.ApiTokenProperty" in config.read_text(encoding="utf-8")


def test_archived_api_token_removal_rejects_a_user_symlink(tmp_path: Path) -> None:
    users = tmp_path / "users"
    users.mkdir()
    archived_user = tmp_path / "archived-user"
    archived_user.mkdir()
    config = archived_user / "config.xml"
    config.write_text(
        "<user><properties><jenkins.security.ApiTokenProperty/></properties></user>\n",
        encoding="utf-8",
    )
    (users / "linked-user").symlink_to(archived_user, target_is_directory=True)

    completed = _run_token_remover(tmp_path)

    assert completed.returncode == 1
    assert completed.stderr.strip() == "archived API token removal failed"
    assert "jenkins.security.ApiTokenProperty" in config.read_text(encoding="utf-8")


def test_reviewed_installation_authority_matches_all_pinned_sources() -> None:
    completed = subprocess.run(
        [
            "sdi-integration",
            "validate-installation",
            "--repository",
            REPOSITORY_ROOT,
            "--installation",
            INSTALLATION,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    authority = INSTALLATION.read_text(encoding="utf-8")
    assert "SDI_SUPPORTED_UBUNTU_LTS=24.04,26.04" in authority
    assert "SDI_REQUIRED_HTTPS_URLS=" in authority
    assert "127.0.0.1" not in authority
    assert "/home/" not in authority


def test_authority_rejects_shadowed_active_pin_assignments(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    required = (
        ".github/workflows/pipeline-integration.yml",
        "deployment/github-runner/bin/runner",
        "deployment/jenkins/adapters/cd-fixture-v1.yaml",
        "deployment/jenkins/adapters/composition-fixture-v1.yaml",
        "deployment/jenkins/adapters/cv-fixture-v1.yaml",
        "deployment/jenkins/adapters/image-build-fixture-v1.yaml",
        "deployment/jenkins/agents/Dockerfile",
        "deployment/jenkins/compose.yaml",
        "deployment/jenkins/controller/Dockerfile",
        "deployment/jenkins/controller/plugins.txt",
        "integration/.python-version",
    )
    for relative in required:
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / relative, target)
    agents = repository / "deployment/jenkins/agents/Dockerfile"
    agents.write_text(
        agents.read_text()
        + "\narg UV_IMAGE=docker.io/example/uv@sha256:"
        + "0" * 64
        + "\n"
    )

    command = [
        "sdi-integration",
        "validate-installation",
        "--repository",
        repository,
        "--installation",
        INSTALLATION,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "uv image pin does not match" in completed.stderr

    shutil.copy2(REPOSITORY_ROOT / "deployment/jenkins/agents/Dockerfile", agents)
    controller = repository / "deployment/jenkins/controller/Dockerfile"
    controller.write_text(
        controller.read_text()
        + "\nfrom\tdocker.io/example/controller@sha256:"
        + "0" * 64
        + "\n"
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "controller pin does not match" in completed.stderr


def test_snapshot_quiesces_then_stops_and_writes_sanitized_manifest(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "operations.log"
    destination = tmp_path / "encrypted-backup"
    destination.mkdir(mode=0o700)
    _write_fake_recovery_tools(fake_bin)

    completed = _run_recovery(
        fake_bin,
        log,
        "--project-name",
        "recovery-test",
        "snapshot",
        "--destination",
        str(destination),
        "--encrypted-destination",
        "--handoff-quiesced",
    )

    assert completed.returncode == 0, completed.stderr
    archive, manifest = map(Path, completed.stdout.splitlines())
    assert archive.parent == destination
    assert manifest.parent == destination
    assert archive.read_bytes() == b"complete-controller-volume"
    document = json.loads(manifest.read_bytes())
    assert document["schema_version"] == "sdi.scaffold-recovery-manifest/v1"
    assert document["archive"] == {
        "sha256": ("08b65d3a16b96e8bda5fcb7edf8e6235e0894e72d146941f3840fbca183209ad"),
        "size_bytes": 26,
    }
    serialized = manifest.read_text(encoding="utf-8")
    assert str(REPOSITORY_ROOT) not in serialized
    assert str(destination) not in serialized
    operations = log.read_text(encoding="utf-8").splitlines()
    assert "volume inspect --format {{.Mountpoint}}" in operations[0]
    assert "preflight" in operations[1]
    assert "quiesce" in operations[2]
    assert "stop" in operations[3]
    assert "tar --create" in operations[4]


def test_snapshot_requires_operator_quiescence_and_external_destination(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "operations.log"
    _write_fake_recovery_tools(fake_bin)

    not_quiesced = _run_recovery(
        fake_bin,
        log,
        "snapshot",
        "--destination",
        str(tmp_path),
        "--encrypted-destination",
    )
    in_checkout = _run_recovery(
        fake_bin,
        log,
        "snapshot",
        "--destination",
        str(REPOSITORY_ROOT / "recovery-artifacts"),
        "--encrypted-destination",
        "--handoff-quiesced",
    )
    volume_destination = tmp_path / "controller-volume" / "backup"
    volume_destination.mkdir(parents=True)
    in_volume = _run_recovery(
        fake_bin,
        log,
        "snapshot",
        "--destination",
        str(volume_destination),
        "--encrypted-destination",
        "--handoff-quiesced",
    )

    assert not_quiesced.returncode == 2
    assert "confirm that workflow handoff is quiesced" in not_quiesced.stderr
    assert in_checkout.returncode == 2
    assert "outside the repository" in in_checkout.stderr
    assert in_volume.returncode == 2
    assert "outside the controller volume" in in_volume.stderr
    assert log.read_text().count("volume inspect --format {{.Mountpoint}}") == 1


def test_restore_verifies_before_writing_an_empty_volume(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    snapshot_log = tmp_path / "snapshot.log"
    destination = tmp_path / "encrypted-backup"
    destination.mkdir(mode=0o700)
    _write_fake_recovery_tools(fake_bin)
    snapshot = _run_recovery(
        fake_bin,
        snapshot_log,
        "--project-name",
        "recovery-test",
        "snapshot",
        "--destination",
        str(destination),
        "--encrypted-destination",
        "--handoff-quiesced",
    )
    assert snapshot.returncode == 0, snapshot.stderr
    archive, manifest = snapshot.stdout.splitlines()
    verified = _run_recovery(
        fake_bin,
        tmp_path / "verify.log",
        "verify",
        "--archive",
        archive,
        "--manifest",
        manifest,
    )
    assert verified.returncode == 0, verified.stderr
    config = _write_fresh_credential_config(tmp_path, Path(manifest))
    first_credential = Path(config.read_text().splitlines()[0].partition("=")[2])
    os.utime(first_credential, (0, 0))
    stale_log = tmp_path / "stale-credentials.log"
    stale = _run_recovery(
        fake_bin,
        stale_log,
        "--project-name",
        "recovery-test",
        "--config",
        str(config),
        "restore",
        "--archive",
        archive,
        "--manifest",
        manifest,
        "--fresh-credentials-ready",
    )
    assert stale.returncode == 2
    assert "fresh credential staging" in stale.stderr
    assert not stale_log.exists()
    created_at = datetime.fromisoformat(
        json.loads(Path(manifest).read_bytes())["created_at"]
    )
    fresh_timestamp = created_at.timestamp() + 1
    os.utime(first_credential, (fresh_timestamp, fresh_timestamp))
    restore_log = tmp_path / "restore.log"

    restored = _run_recovery(
        fake_bin,
        restore_log,
        "--project-name",
        "recovery-test",
        "--config",
        str(config),
        "restore",
        "--archive",
        archive,
        "--manifest",
        manifest,
        "--fresh-credentials-ready",
    )

    assert restored.returncode == 0, restored.stderr
    operations = restore_log.read_text(encoding="utf-8").splitlines()
    assert "preflight" in operations[0]
    assert "volume inspect recovery-test_jenkins-home" in operations[1]
    assert "find /target -mindepth 1 -print -quit" in operations[2]
    _assert_restore_credential_rotation_order(operations)
    config_values: dict[str, str] = {}
    for line in config.read_text().splitlines():
        key, _, value = line.partition("=")
        config_values[key] = value
    assert Path(config_values["JENKINS_INTEGRATION_AGENT_SECRET_FILE"]).read_text() == (
        "a" * 64 + "\n"
    )

    Path(archive).write_bytes(b"tampered")
    rejected_log = tmp_path / "rejected.log"
    rejected = _run_recovery(
        fake_bin,
        rejected_log,
        "--project-name",
        "recovery-test",
        "--config",
        str(config),
        "restore",
        "--archive",
        archive,
        "--manifest",
        manifest,
        "--fresh-credentials-ready",
    )
    assert rejected.returncode == 2
    assert "archive does not match its recovery manifest" in rejected.stderr
    assert not rejected_log.exists()


def test_runner_interface_covers_validation_and_removal_lifecycle() -> None:
    script = RUNNER.read_text(encoding="utf-8")

    assert "deployment/installation.env" in script
    assert all(
        f"{command})" in script
        for command in (
            "preflight",
            "install",
            "configure",
            "install-service",
            "start",
            "stop",
            "status",
            "unregister",
            "remove-service",
            "remove",
        )
    )
    assert "SDI_GITHUB_RUNNER_REMOVAL_TOKEN" in script
    assert "systemctl --user disable" in script
    assert "--disableupdate" in script


def test_quiesce_refuses_an_active_run_and_cancels_quiet_mode(tmp_path: Path) -> None:
    events: list[str] = []

    class ActiveJenkinsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/crumbIssuer/api/json":
                body = b'{"crumbRequestField":"Jenkins-Crumb","crumb":"test"}'
            elif self.path.startswith("/queue/api/json"):
                body = b'{"items":[{"id":42}]}'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            events.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *arguments: object) -> None:  # noqa: A002
            del format, arguments

    password = tmp_path / "admin-password"
    password.write_text("not-printed\n", encoding="utf-8")
    password.chmod(0o600)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ActiveJenkinsHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        completed = subprocess.run(
            [
                SMOKE_CHECK,
                "quiesce",
                "--url",
                f"http://127.0.0.1:{server.server_port}",
                "--admin-id",
                "administrator",
                "--admin-password-file",
                password,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert completed.returncode == 1
    assert "active-run: Jenkins queue is not empty" in completed.stderr
    assert "not-printed" not in completed.stderr
    assert events == ["/quietDown", "/cancelQuietDown"]

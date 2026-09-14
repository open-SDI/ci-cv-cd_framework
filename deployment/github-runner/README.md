# GitHub Handoff Runner

This directory owns the host interface for the one self-hosted runner permitted
to hand a Pipeline integration run to private Jenkins. The runner is pinned to
GitHub Actions runner `2.337.0` for Linux x86_64 and the upstream archive's
published SHA-256 digest. It is configured with automatic binary updates
disabled so a reviewed pin cannot drift.

## Account Boundary

Create a dedicated unprivileged account on the Jenkins host. The account must:

- not be `root` and not belong to `sudo`, `wheel`, or `docker`;
- have no readable or writable Docker socket;
- have no access to Jenkins controller state, bootstrap secrets, agent
  registration secrets, or backup files;
- own only its home, runner installation, and runner work directories;
- have outbound HTTPS access to GitHub, Python downloads, and PyPI;
- reach Jenkins only through the configured loopback endpoint.

Install the runner's documented native dependencies and enable lingering for
this account as a separate host-administrator operation. The tracked interface
does not elevate privileges. All installation and service commands below run as
the dedicated account and use `systemctl --user`.

## Installation

From the repository checkout, run:

```sh acceptance=runner-install
deployment/github-runner/bin/runner preflight
deployment/github-runner/bin/runner install
```

`preflight` verifies the dedicated account boundary, supported Ubuntu LTS
x86_64 host, Git, systemd user manager, native archive tools, writable private
installation area, required HTTPS access, and the runner pin in the shared
`deployment/installation.env` authority. Failures name the missing capability
without printing account state, private paths, credentials, or tool output. A
host administrator, not this interface, installs native dependencies and enables
user lingering.

`install` downloads exactly
`actions-runner-linux-x64-2.337.0.tar.gz`, verifies digest
`70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613`,
and installs into `~/.local/share/sdi-github-runner`.

Create a short-lived repository runner registration token, then configure the
installation once:

```sh acceptance=runner-configure-start
SDI_GITHUB_RUNNER_REPOSITORY=https://github.com/open-SDI/ci-cv-cd_framework \
SDI_GITHUB_RUNNER_NAME=jenkins-handoff-host \
SDI_GITHUB_RUNNER_REGISTRATION_TOKEN='<short-lived-registration-token>' \
deployment/github-runner/bin/runner configure

deployment/github-runner/bin/runner install-service
deployment/github-runner/bin/runner start
deployment/github-runner/bin/runner status
```

The registration token is configuration-time input, not a service environment
value. The runner receives only the custom label `sdi-jenkins-handoff`; default
labels are disabled. Automatic updates are disabled. The user service enables
`NoNewPrivileges`, a read-only host filesystem with only runner diagnostics and
job work paths writable, private devices and temporary files, and explicit
removal of Jenkins credential and state variables from its persistent
environment. The workflow places uv's cache, managed Python installation, and
Zig's global build cache under the writable runner temporary directory.

The supported installation uses a repository-scoped runner and treats its label
as an operational selector, not an authorization control. It requires trusted
write collaborators, reviewed protected-main changes, approval for every
external fork workflow, no approval of untrusted workflow code, and a repository
rule that no other workflow may target `sdi-jenkins-handoff`. An organization
workflow-restricted runner group requires a separately reviewed extension to
the runner interface; repository-scoped `configure` does not select one.

Stop the service without removing registration or work state with:

```sh acceptance=runner-stop
deployment/github-runner/bin/runner stop
```

For permanent removal, first confirm no workflow is queued or running and stop
the service. Create a short-lived removal token, then unregister before removing
the service and installation:

```sh acceptance=runner-remove
SDI_GITHUB_HANDOFF_QUIESCED=true \
SDI_GITHUB_RUNNER_REMOVAL_TOKEN='<short-lived-removal-token>' \
  deployment/github-runner/bin/runner unregister
SDI_GITHUB_HANDOFF_QUIESCED=true deployment/github-runner/bin/runner remove-service
SDI_GITHUB_HANDOFF_QUIESCED=true deployment/github-runner/bin/runner remove
```

The quiescence acknowledgement is required by every destructive operation; it
does not replace checking the exact GitHub run. The removal token is never
persisted. `remove` refuses a registered installation
or installed service. Repository access, collaborator policy, runner
registration/removal tokens, host accounts, native packages, firewall rules,
SSH, and user lingering remain guided administrator work.

## Replacement

Replace a runner one at a time while no Pipeline integration run is active. Use
a separate candidate low-privilege account on the same host so both installations
remain independently manageable through this interface:

1. Stop the old runner, but keep its registration, service, installation, and
   account intact for rollback.
2. Review and commit the new version and digest in
   `deployment/installation.env` and this interface as one change.
3. From the candidate account, run preflight, then install, configure, and start
   the candidate with a distinct runner name and the same handoff-only label.
4. Confirm exactly one online repository runner has only the
   `sdi-jenkins-handoff` label and remains low privilege; the old runner must
   remain offline.
5. Run one protected-main Fixture through `Pipeline integration` and retain its
   exact GitHub run URL.
6. Only after validation succeeds, use the old account to unregister and remove
   its service and files, then retire that account through guided administration.

Do not run old and new runners concurrently against the handoff label. There is
no automatic update or credential-rotation mechanism.

The runner software must be reviewed and repinned within 30 days of a new
GitHub Actions runner release. A critical security update can require an
immediate replacement before GitHub will schedule more work.

Jenkins's machine-user API token is never configured on this host interface.
Store it only in the protected GitHub environment; GitHub injects it into the
one approved job for that job's lifetime.

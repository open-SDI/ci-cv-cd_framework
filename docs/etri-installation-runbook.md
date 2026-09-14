# ETRI Installation Runbook

This runbook reconstructs the Pipeline integration scaffold from
`open-SDI/ci-cv-cd_framework` on an ETRI-managed host. It does not transfer an
existing installation's Jenkins state or credentials. Select an approved commit
from the upstream protected `main` branch after merge and record that exact SHA
during acceptance.

The installed system runs deterministic Fixture adapters. It proves the
GitHub-to-Jenkins pipeline interface, not real composition, image publication,
Continuous Validation, deployment, or KPI results.

## Responsibilities

| Role | Responsibility |
| --- | --- |
| Repository administrator | Protect `main`, configure the GitHub environment, and issue runner tokens |
| Host administrator | Prepare Ubuntu, accounts, packages, network policy, and encrypted backup storage |
| Stack operator | Configure and operate Jenkins and its five agents |
| ETRI operator | Trigger runs in GitHub and retrieve artifacts; no Jenkins or host access is required |

Keep the stack operator and runner as separate OS users. The stack operator
needs Docker access. The runner must be non-root, outside `sudo`, `wheel`, and
`docker`, unable to access the Docker socket, Jenkins state, or secret files,
and have a working systemd user manager with lingering enabled.

## 1. Prepare The Host

Use Ubuntu LTS 24.04 or 26.04 on x86_64 with:

- Docker Engine 27 or newer and Docker Compose v2, available to the stack
  operator without privilege escalation;
- Git 2 or newer, Python 3, `uv` 0.12.1, systemd, `curl`, `sha256sum`, and
  `tar`;
- the native libraries required by GitHub Actions runner 2.337.0;
- outbound HTTPS access to every URL listed in
  `deployment/installation.env`;
- an unused loopback port for Jenkins and sufficient local container storage.

Install these through ETRI's approved package and account-management process.
The repository intentionally does not modify the OS, firewall, SSH, accounts,
or Docker installation. This keeps host security policy under ETRI control.

To install the pinned runner's native dependencies without executing a
runner-owned file as root, the host administrator uses Bash and a separate
staging area:

```bash
set -euo pipefail
staging="$(mktemp -d)"
archive="$staging/actions-runner-linux-x64-2.337.0.tar.gz"
curl --fail --location --proto '=https' --tlsv1.2 --output "$archive" \
  https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz
printf '%s  %s\n' \
  70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613 \
  "$archive" | sha256sum --check
mkdir "$staging/runner"
tar --extract --gzip --file "$archive" --directory "$staging/runner"
sudo "$staging/runner/bin/installdependencies.sh"
rm -rf "$staging"
```

## 2. Prepare The Repository

Create a fresh, publicly readable clone of
`https://github.com/open-SDI/ci-cv-cd_framework.git` with that uncredentialed
HTTPS URL as its sole `origin`. Check out the approved protected-main revision
and leave the worktree completely clean, including no untracked files. Jenkins
agents intentionally receive no source credential.

Run the repository acceptance check on the supported Docker host:

```sh
integration/scripts/verify
```

Do not copy `/tmp` helpers, local configuration, credentials, artifacts, or an
existing `jenkins-home` volume. The tracked scripts and immutable pins are the
installation authority.

## 3. Configure Jenkins

Create the ignored local configuration:

```sh
deployment/jenkins/bin/stack setup
```

Edit `deployment/jenkins/config/local/controller.env`. Set every value from the
generated example. Use:

```text
JENKINS_REPOSITORY_URL=https://github.com/open-SDI/ci-cv-cd_framework.git
JENKINS_AGENT_CONTROLLER_URL=http://controller:8080
```

Select distinct Jenkins administrator and handoff-machine-user IDs. Create two
different bootstrap passwords in owner-only files outside the checkout. A
portable way to generate each file is:

```sh
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > /secure/path/password
```

Configure five distinct, initially absent secret-file paths outside the
checkout for `integration`, `ci`, `image-build`, `cv`, and `cd`. Their parent
directory must be writable only by the stack operator.

Validate the host and configuration:

```sh
deployment/jenkins/bin/stack preflight
deployment/jenkins/bin/stack start-controller
```

Sign in to loopback Jenkins as the administrator. Under **Manage Jenkins >
Nodes**, open each of the five nodes and write its displayed inbound
registration secret to the matching configured file with mode `0600`. Never
reuse a node secret.

Start and validate the complete stack:

```sh
deployment/jenkins/bin/stack start
deployment/jenkins/bin/stack verify-live
deployment/jenkins/bin/stack exercise-live
```

Jenkins remains at `http://127.0.0.1:<configured-port>`. Do not publish it or an
inbound-agent TCP port. The agents connect over WebSocket on isolated Docker
networks.

## 4. Install The GitHub Runner

As the dedicated runner user, from a separate clean checkout:

```sh
deployment/github-runner/bin/runner preflight
deployment/github-runner/bin/runner install
```

In GitHub, open **Settings > Actions > Runners > New self-hosted runner** and
create a short-lived repository registration token. Configure and start the
runner:

```sh
SDI_GITHUB_RUNNER_REPOSITORY=https://github.com/open-SDI/ci-cv-cd_framework \
SDI_GITHUB_RUNNER_NAME=jenkins-handoff-host \
SDI_GITHUB_RUNNER_REGISTRATION_TOKEN='<short-lived-token>' \
deployment/github-runner/bin/runner configure

deployment/github-runner/bin/runner install-service
deployment/github-runner/bin/runner start
deployment/github-runner/bin/runner status
```

Confirm that exactly one online runner has only the custom label
`sdi-jenkins-handoff`. The token is configuration-time input and must not be
stored in the service environment.

## 5. Configure GitHub

Protect `main` against direct changes, force pushes, and deletion. Require pull
request review and external-fork workflow approval. Permit no other workflow to
target `sdi-jenkins-handoff`.

Create the protected environment `pipeline-integration-jenkins`, restrict it to
protected branches, and add reviewers if ETRI policy requires approval. Add:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `SDI_JENKINS_BASE_URL` | `http://127.0.0.1:<configured-port>` |
| Variable | `SDI_JENKINS_JOB_PATH` | `pipeline-integration` |
| Variable | `SDI_JENKINS_REPOSITORY` | `github.com/open-SDI/ci-cv-cd_framework` |
| Variable | `SDI_JENKINS_USERNAME` | Handoff-machine-user ID |
| Secret | `SDI_JENKINS_API_TOKEN` | Named API token issued for that user |

Issue the API token in the handoff user's Jenkins configuration while signed in
as the administrator. Use an API token, not either bootstrap password. GitHub
delivers it only to the protected job; it must not be placed in the checkout,
runner service, command line, or readiness evidence.

The supported installation uses a repository-scoped runner and treats its label
as an operational selector, not an authorization boundary. Trust every write
collaborator and reviewer who can approve protected-main or external-fork
workflow code. An organization workflow-restricted runner group requires a
separately reviewed extension to the runner interface before it can replace this
configuration.

## 6. Accept The Installation

In GitHub, open **Actions > Pipeline integration > Run workflow**, select
`main`, and submit:

```text
runs/s-04/s-04-tc-03-c-01-fixture.yaml
```

Record the accepted upstream commit SHA and the exact run and artifact URLs. On
a validation workstation with a clean repository checkout, `uv` 0.12.1, and
`gh` authenticated to the repository, download the Execution-ID-qualified
artifact and validate its complete bundle:

```sh
gh run download <workflow-run-id> \
  --repo open-SDI/ci-cv-cd_framework \
  --name pipeline-integration-<execution-id> \
  --dir artifacts/pipeline-integration-<execution-id>

uv run --frozen --project integration sdi-integration validate-bundle \
  --bundle-root artifacts/pipeline-integration-<execution-id>/bundle
```

Complete `docs/installation-readiness-checklist.md`. Do not record credentials,
private paths, hostnames, Jenkins console output, or archive contents.

## Operation

Before shutdown, stop the runner and prevent new environment delivery. Then:

```sh
deployment/jenkins/bin/stack quiesce
deployment/jenkins/bin/stack stop
```

Use `stack start`, `verify-live`, and then `runner start` to resume. Follow
`deployment/jenkins/README.md` for snapshot, restore, reconstruction, and
one-at-a-time credential or agent replacement.

## Design Rationale

- **GitHub is the only external surface:** ETRI operators need one familiar,
  auditable trigger and artifact interface; Jenkins remains private.
- **Loopback-only Jenkins:** the handoff runner can reach Jenkins without adding
  an externally exposed control plane, reverse proxy, or TLS credential.
- **Separate identities:** administrator, handoff user, runner, integration
  agent, and four Domain agents receive only the authority required for their
  roles.
- **Generated local credentials:** no installation secret is portable or safe
  to commit. Reconstruction creates destination-owned passwords, tokens, and
  node registrations.
- **Immutable pins and clean checkout:** the accepted upstream commit, image
  digests, plugin lock, and dependency lock make the software inputs reviewable
  and repeatable.
- **Five isolated agents:** each Stage has a replaceable runtime, one executor,
  its own registration secret, and a disposable workspace rather than shared
  mutable state.
- **Manual host provisioning:** ETRI retains control of operating-system,
  network, account, storage-encryption, and package policy; preflight verifies
  the capabilities the scaffold actually needs.

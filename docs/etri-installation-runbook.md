# ETRI Installation Runbook

This runbook reconstructs the Pipeline integration scaffold from
`open-SDI/ci-cv-cd_framework` on an ETRI-managed host. It does not transfer an
existing installation's Jenkins state or credentials. Select an approved commit
from the upstream protected `main` branch after merge and record that exact SHA
during acceptance.

The installed system runs real composition, image-build, and Continuous
Validation (CV) Stages, and a deterministic CD Fixture. It proves the
GitHub-to-Jenkins pipeline interface, model-backed composition, image
publication to the stack-local registry, and one CV verdict per run. It does not
prove deployment or KPI results.

## Responsibilities

| Role | Responsibility |
| --- | --- |
| Repository administrator | Protect `main`, configure the GitHub environment, and issue runner registration tokens |
| Host administrator | Prepare Ubuntu, accounts, packages, network policy, and encrypted backup storage |
| Stack operator | Configure and operate Jenkins, its five agents, and the GitHub handoff runner |
| ETRI operator | Trigger runs in GitHub and retrieve artifacts; no Jenkins or host access is required |

The stack operator needs Docker access. The GitHub runner needs no OS account:
it is a stack container that runs as a non-root user without `sudo`, the Docker
socket, Jenkins state, or secret files, and the stack operator registers it.

## 1. Prepare The Host

Use Ubuntu LTS 24.04 or 26.04 on x86_64 with:

- Docker Engine 27 or newer and the Docker Compose plugin 2.20.2 or newer,
  available to the stack operator without privilege escalation;
- Git 2 or newer, Python 3, `uv` 0.12.1, systemd, `curl`, `sha256sum`, and
  `tar`;
- the GitHub CLI `gh`, to pipe a runner registration token into the stack;
- outbound HTTPS access to every URL listed in
  `deployment/installation.env`;
- an unused loopback port for Jenkins and sufficient local container storage.

Install these through ETRI's approved package and account-management process.
The repository intentionally does not modify the OS, firewall, SSH, accounts,
or Docker installation. This keeps host security policy under ETRI control.

The host needs no GitHub Actions runner libraries. The runner image build
downloads the pinned runner release, checks its SHA-256, and installs its
dependencies inside the image.

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
JENKINS_FORK_REPOSITORY_URL=https://github.com/<fork-owner>/ci-cv-cd_framework.git
JENKINS_AGENT_CONTROLLER_URL=http://controller:8080
```

`JENKINS_FORK_REPOSITORY_URL` must name a different repository, normally the
maintainer's fork. The fork job `pipeline-integration-fork` stays idle unless a
fork runner is registered; this runbook installs only the upstream runner
service. Before any upstream dispatch, a sync pull request must bring
upstream `main` level with the fork `main` commit the agent images were built
from.

Select distinct Jenkins administrator and handoff-machine-user IDs. Create two
different bootstrap passwords in owner-only files outside the checkout. A
portable way to generate each file is:

```sh
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > /secure/path/password
```

Write the OpenAI API key, and nothing else, to a third owner-only file outside
the checkout and name it in `JENKINS_OPENAI_API_KEY_FILE`. JCasC turns it into
the `sdi-openai-api-key` credential that the composition Stage uses.

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

On a new `cv-engine-state` volume, `start` also builds the CV simulation image
into the CV engine before it starts the agents. Measured builds took 675 to
4400 s. `start` prints each `Step` line of the build and then
`sdi-cv-warm: sdi-cv-sim:<hash> built in <seconds> s`. If the build fails, for
example because a package mirror keeps returning errors, `start` exits with an
error and leaves the agents stopped. Run `deployment/jenkins/bin/stack start`
again. A later `start` finds the image and prints `present in`.

Jenkins remains at `http://127.0.0.1:<configured-port>`. Do not publish it or an
inbound-agent TCP port. The agents connect over WebSocket on isolated Docker
networks.

## 4. Install The GitHub Runner

Add `GITHUB_RUNNERS=upstream` to
`deployment/jenkins/config/local/controller.env`. With the stack running, a
repository administrator creates a one-hour registration token, and the stack
operator pipes it into `register-runner` with a runner name of ETRI's choice,
then starts and validates the stack:

```sh
gh api -X POST repos/open-SDI/ci-cv-cd_framework/actions/runners/registration-token \
  --jq .token | deployment/jenkins/bin/stack register-runner upstream \
  --name etri-jenkins-handoff
deployment/jenkins/bin/stack start
deployment/jenkins/bin/stack verify-live
```

`register-runner` refuses a token typed at a terminal and never prints it. The
token is registration-time input only and is never stored by the stack.
Confirm that exactly one online runner in the repository has only the custom
label `sdi-jenkins-handoff`.

GitHub assigns a queued job to any online runner with that label, so only one
installation may have an online upstream runner. When ETRI's runner goes live,
the development host's upstream runner must be stopped by setting its
`GITHUB_RUNNERS=fork` and restarting its stack.

## 5. Configure GitHub

Protect `main` against direct changes, force pushes, and deletion. Require pull
request review and external-fork workflow approval. Permit no other workflow to
target `sdi-jenkins-handoff`.

Create the protected environment `pipeline-integration-jenkins`, restrict it to
protected branches, and add reviewers if ETRI policy requires approval. Add:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `SDI_JENKINS_JOB_PATH` | `pipeline-integration` |
| Variable | `SDI_JENKINS_REPOSITORY` | `github.com/open-SDI/ci-cv-cd_framework` |
| Variable | `SDI_JENKINS_USERNAME` | Handoff-machine-user ID |
| Secret | `SDI_JENKINS_API_TOKEN` | Named API token issued for that user |

The workflow hard-codes the Jenkins base URL `http://controller:8080`, so the
environment has no `SDI_JENKINS_BASE_URL` variable.

Issue the API token in the handoff user's Jenkins configuration while signed in
as the administrator. Use an API token, not either bootstrap password. GitHub
delivers it only to the protected job; it must not be placed in the checkout,
runner container, command line, or readiness evidence.

The supported installation uses a repository-scoped runner and treats its label
as an operational selector, not an authorization boundary. Trust every write
collaborator and reviewer who can approve protected-main or external-fork
workflow code. An organization workflow-restricted runner group requires a
separately reviewed extension to the runner service before it can replace this
configuration. The runner's outbound traffic passes only through its own
proxy, which admits only the GitHub Actions endpoints; the proxy limits where
job code can connect but does not prevent data exfiltration. See the security
review in `deployment/jenkins/README.md`.

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

Before shutdown, prevent new environment delivery. Then quiesce, which also
stops the runner, and stop the stack:

```sh
deployment/jenkins/bin/stack quiesce
deployment/jenkins/bin/stack stop
```

Use `stack start` and `verify-live` to resume; `start` also starts the runner.
After `stack destroy` or a deleted `cv-engine-state` volume, the next `start`
builds the CV simulation image again, as in section 3. Follow
`deployment/jenkins/README.md` for snapshot, restore, reconstruction, and
one-at-a-time credential or agent replacement.

## Design Rationale

- **GitHub is the only external surface:** ETRI operators need one familiar,
  auditable trigger and artifact interface; Jenkins remains private.
- **Loopback-only Jenkins:** Jenkins is still published only on loopback; the
  handoff runner reaches it over an internal Compose network without an
  externally exposed control plane, reverse proxy, or TLS credential.
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

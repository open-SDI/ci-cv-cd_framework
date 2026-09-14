# Jenkins Deployment

This directory owns the reconstructable Jenkins control plane and five inbound
agents. The controller runs on supported Ubuntu LTS x86_64, has no executors,
and is started only on demand. One dedicated `integration` agent performs only
integration work; four independently replaceable agents provide the
`composition`, `image-build`, `cv`, and `cd` scheduling labels. The root pipeline
executes one submitted Fixture chain across those five agents.

The tracked authority is:

- `controller/Dockerfile`: Jenkins `2.568.3` on JDK 21, pinned to the reviewed
  Linux/amd64 image digest.
- `controller/plugins.txt`: the complete 47-plugin runtime set, including every
  transitive dependency at an exact version.
- `agents/Dockerfile`: the exact Jenkins Remoting, Python 3.12.13, `uv`, and
  locked production integration package used to construct each minimal agent
  image without inherited volume metadata.
- `casc/jenkins.yaml`: global security, zero-executor, five-node, retention, and
  Job DSL configuration.
- `jobs/pipeline-integration.groovy`: the fixed parameterized Pipeline SCM job.
- `compose.yaml`: loopback-only controller publication, named controller state,
  five agent services, and a separate egress-capable network for each agent.
- `Jenkinsfile`: the thin repository-owned scheduler over public integration CLI
  operations.
- `bin/stack`: the supported validation and lifecycle interface.
- `bin/recovery`: the supported cold snapshot, verification, and restore
  interface.
- `../installation.env`: reviewed non-secret host support, upstream reachability,
  and exact-pin authority shared with the runner and recovery interfaces.

Jenkins Configuration as Code and Job DSL run on every controller startup. A
restart therefore restores tracked global and job configuration after mutable UI
drift. Runtime history and Jenkins-generated state persist in the named volume,
but they are not configuration authority.

## Host Requirements

- Ubuntu LTS on x86_64.
- Docker Engine with the Compose v2 plugin, usable by the operator without
  privilege escalation.
- Python 3 for the isolated smoke-check client.
- `uv` for strict adapter-descriptor validation and runtime-image selection.
- Outbound HTTPS access to Docker Hub, GHCR, the Jenkins update service during a
  clean image build, PyPI during a clean agent build, and GitHub for each
  agent's independent immutable checkout.

Supported releases and minimum tool majors are explicit in
`deployment/installation.env`. The tracked setup does not install or update the
operating system, Docker, Compose, Git, systemd, firewall, SSH, or accounts.

`bin/stack validate` verifies the live Docker platform, secret ownership and
permissions, supplied values, Dockerfile syntax, and the fully interpolated
Compose model. It intentionally does not install Docker, alter the firewall, or
enable a host-boot service.

## Setup And Preflight

Run the scaffold-only setup and complete host preflight from the approved
protected-main checkout:

```sh acceptance=stack-setup-preflight
deployment/jenkins/bin/stack setup
deployment/jenkins/bin/stack preflight
```

`setup` creates only the private local configuration directory and a private
copy of the tracked example when one is absent. Edit that local file, create its
two controller password files, and select five writable future agent-secret
paths outside the checkout before running `preflight`.
The preflight verifies supported Ubuntu LTS x86_64, Docker Engine and Compose,
systemd, Git, `uv`, required HTTPS access, checkout/configuration permissions,
secret permissions, required Docker capabilities, Compose interpolation, and
that every tracked software pin still agrees with `deployment/installation.env`.
Diagnostics are capability-oriented and suppress raw network and tool output.

Host administrators separately own OS and Docker installation, firewall and SSH
policy, administrator and low-privilege accounts, GitHub protection and
collaborators, Jenkins identity approval, token provisioning, and credential
rotation decisions. The scaffold never uses privilege escalation to perform
those tasks.

## Local Configuration

Copy `config/controller.env.example` to the ignored
`config/local/controller.env` and set every value. The repository URL must be an
uncredentialed HTTPS GitHub clone URL. `JENKINS_AGENT_CONTROLLER_URL` is
exactly `http://controller:8080` for local Compose agents and an uncredentialed
HTTPS URL for a relocated agent. Persisted container IPs and machine names are
not configuration. The host port, user IDs, endpoint, repository identity, and
secret locations are local deployment settings rather than tracked topology.
The five `*_LIMIT_SECONDS` settings are trusted deployment overrides for the
tracked 90-minute run and 10, 30, 45, and 15 minute Stage defaults. They must be
positive integers no greater than one day; the run limit must leave at least two
minutes for finalization. Workflow dispatch parameters cannot change them.

Create the administrator and machine-user password files outside the checkout.
Each file must be nonempty, owned by the operator, and inaccessible to group and
other users, for example mode `0600`. Compose mounts them through `/run/secrets`;
secret values are never placed in tracked files or Compose environment values.

JCasC creates the two local identities from those bootstrap files. The
administrator has `Overall/Administer`. The handoff machine user has only
`Overall/Read` globally and `Job/Read`, `Job/Build`, and `Job/Cancel` on the fixed
job. Anonymous users receive no permissions. The administrator must later issue
the named machine-user API token and place it only in the protected
`pipeline-integration-jenkins` GitHub environment. Password authentication is
not the GitHub-to-Jenkins contract.

The dedicated GitHub runner uses the host interface under
`deployment/github-runner/`. It is a separate low-privilege user service with no
Docker socket or Jenkins-state access. The service environment never persists
this API token; GitHub releases it only to the protected handoff job.

JCasC owns one reviewed script-sandbox approval: reading
`FlowInterruptedException.getCauses()` lets the thin scheduler distinguish a
Jenkins timeout from external cancellation. No other custom signature is
approved.

JCasC creates five permanent inbound node identities. Their physical names and
pipeline-facing labels are:

| Node | Explicit label | Executors | Responsibility |
| --- | --- | ---: | --- |
| `integration` | `integration` | 1 | Revalidation, preflight, assembly, bundle validation, and archive preparation |
| `ci` | `composition` | 1 | Composition Stage adapter |
| `image-build` | `image-build` | 1 | Image-build Stage adapter |
| `cv` | `cv` | 1 | CV Stage adapter |
| `cd` | `cd` | 1 | CD Stage adapter |

Each node has its own controller-generated registration secret. For a new or
destroyed controller volume:

1. Configure five distinct future secret-file paths outside the checkout.
2. Run `deployment/jenkins/bin/stack start-controller`.
3. In **Manage Jenkins > Nodes**, open each node and write its displayed inbound
   registration secret to the corresponding configured file with mode `0600`.
4. Run `deployment/jenkins/bin/stack start` to validate the distinct secrets,
   build the agents, and connect all five over WebSocket.

Never copy one registration secret between nodes. Destroying `jenkins-home`
invalidates the old files; remove them and repeat provisioning. The integration
agent receives no Domain credential and has no Domain label. The four Fixture
descriptors currently declare no Domain secret binding, so every agent container
sees only its own registration file and no controller password.

Each image contains an immutable role and label identity under `/etc/sdi` on its
read-only root filesystem. The supported Stage execution commands refuse every
Domain adapter on the `integration` image and require a Domain image's label to
match the selected descriptor before starting a child process. Integration-only
preflight and finalization likewise reject a Domain image. Job environment
changes cannot alter that boundary. Local execution outside an agent image has no
deployment role and remains available for development.

## Operation

Run from any directory:

```sh acceptance=stack-lifecycle
deployment/jenkins/bin/stack validate
deployment/jenkins/bin/stack start
deployment/jenkins/bin/stack status
deployment/jenkins/bin/stack logs
deployment/jenkins/bin/stack exercise-live
deployment/jenkins/bin/stack stop
```

`start` builds the exact controller and five agent images, starts all six
containers, rejects an agent image that declares any volume, and waits for
process health. Jenkins is available only at
`http://127.0.0.1:<configured-port>`. No inbound-agent TCP port is published;
all agents connect over WebSocket. `stop` removes all containers and networks
but retains only the `jenkins-home` named volume. Agent workspaces are separate
tmpfs mounts, have no shared or persistent filesystem, and disappear whenever
their container is replaced. Their five separate networks allow GitHub checkout
without making peer agents reachable.

The root pipeline deletes every allocated workspace before checkout and in a
`finally` block. Each agent independently checks out and verifies the submitted
protected-main commit. Accepted envelopes and their declared bounded files are
the only cross-agent Stage data, transferred through Execution-ID-qualified
Jenkins stashes. The integration agent validates the complete bundle before
archiving all and only its declared files from the current build.

The pipeline defaults to 10, 30, 45, and 15 minute Python-owned Stage work
limits, a 90-minute Jenkins run limit, and a two-minute finalization limit. The
validated local deployment settings may override the Stage and run limits. A
handled machinery failure archives its structured result before Jenkins ends in
`FAILURE`; a contract-valid negative Domain outcome can remain `SUCCESS`.
External cancellation propagates immediately, cleans active workspaces and
process trees, ends as `ABORTED`, and does not manufacture a result.

Each Domain service owns its descriptor-selected `RUNTIME_IMAGE` build input and
local image tag. `bin/stack` loads each image through the integration CLI's strict
descriptor model and injects it only into the matching Compose service. A
reviewed descriptor image replacement followed by
`bin/stack reconcile-agent <ci|image-build|cv|cd>` builds and replaces only the
affected Domain agent without starting or rebuilding the controller. The node
identity and scheduling label remain unchanged. Only the selected agent's
registration secret is validated or mounted during reconciliation.

`bin/stack destroy` is the explicit destructive operation for reconstruction. It
removes the controller and its named volume. Back up operational state first if
it must be retained.

## Safe Shutdown

Prevent new protected-environment delivery and stop the dedicated GitHub runner
before shutdown. Then quiesce Jenkins and stop the stack:

```sh acceptance=stack-safe-shutdown
deployment/jenkins/bin/stack quiesce
deployment/jenkins/bin/stack stop
```

`quiesce` asks Jenkins to stop accepting new work and refuses while the fixed
job has queued or building work. If it observes active work, it cancels quiet
mode and leaves the stack running. Do not use `stop` directly during an active
run. `stop` gives every container 60 seconds to terminate, rejects a forced
SIGKILL shutdown, and only then removes the containers. Resume an intentionally
quiesced live controller with `stack resume`; after a full shutdown, start the
stack and then the runner after validation.

## Backup And Restore

Snapshots are manual, cold, and sensitive. Select an operator-encrypted
destination outside both the checkout and Docker volume, stop the runner and
protected-environment handoff, and run:

```sh acceptance=recovery-snapshot
deployment/jenkins/bin/recovery snapshot \
  --destination /operator/encrypted/off-host-location \
  --encrypted-destination \
  --handoff-quiesced
```

The acknowledgement flags record operator decisions; the scaffold cannot prove
storage encryption or GitHub-side quiescence. The command then quiesces Jenkins,
refuses queued or building runs, cleanly stops all six containers, and archives
the complete `jenkins-home` named volume with numeric ownership. It creates an
owner-only archive and a path-free recovery manifest containing the approved
commit, UTC timestamp, byte size, SHA-256, and every exact installation pin.
Both outputs must remain encrypted and outside the repository. There is no
scheduler, retention service, upload service, RPO, or RTO.

Verify an archive without changing Docker state:

```sh acceptance=recovery-verify
deployment/jenkins/bin/recovery verify \
  --archive /operator/encrypted/off-host-location/jenkins-home-....tar.gz \
  --manifest /operator/encrypted/off-host-location/jenkins-home-....manifest.json
```

Restore only from the exact approved commit recorded in the manifest. Stage new
administrator and machine-user bootstrap passwords after the snapshot was
created, and configure five distinct writable agent-secret paths outside the
checkout. The restore interface checks the controller files' modification
times, nonempty values, password separation, and every destination path before
touching the target volume. The acknowledgement flag records the explicit
operator decision to replace the archived credentials; it is not a scheduler or
general credential-rotation service:

```sh acceptance=recovery-restore
deployment/jenkins/bin/recovery restore \
  --archive /operator/encrypted/off-host-location/jenkins-home-....tar.gz \
  --manifest /operator/encrypted/off-host-location/jenkins-home-....manifest.json \
  --fresh-credentials-ready
```

Restore verifies the manifest schema, archive size and digest, commit, and all
pins before touching Docker. It refuses a nonempty target volume. Before the
target restore, it creates a private disposable volume from the archive, removes
every archived user API token and Jenkins's inbound-agent HMAC key there, starts
the controller with the new identity passwords, asks Jenkins to issue five
replacement registration secrets, and starts all agents. `verify-live` proves
those successor credentials and repository convergence before the external
files are replaced or the target receives the replacement HMAC key. The
disposable stack is then stopped and removed. The target archive is restored,
its archived user API tokens are removed, and the proven replacement HMAC key is
installed before the exact stack starts and convergence is proved again.
Archived controller and agent credentials therefore never become active on the
target. Before restarting the runner or protected-environment handoff, the
administrator must issue a successor token for the handoff machine user, replace
`SDI_JENKINS_API_TOKEN` in the protected `pipeline-integration-jenkins`
environment, and perform the documented machine-user preflight without exposing
the token. A failed target extraction leaves a partial volume that must be
destroyed before retrying. Keep the archive until restoration and the
protected-main Fixture check are accepted.

## Clean Reconstruction

Reconstruction is distinct from restoration and starts with no controller
history:

1. Approve and check out one protected-main commit; run `stack setup` and
   `stack preflight`.
2. Confirm no run is active, retain any required snapshot, run `stack stop`, and
   explicitly run `stack destroy`.
3. Stage fresh administrator and handoff bootstrap passwords and five future
   agent-secret paths outside the checkout.
4. Run `stack start-controller`, provision the five fixed node registration
   secrets, and run `stack start`.
5. Run `stack verify-live` and `stack exercise-live` against the reconstructed
   installation. These prove the zero-executor controller, fixed job, five
   identities and labels, one executor per agent, clean isolated workspaces,
   exact pins, and convergence. Run `stack smoke` separately for repository
   acceptance; it uses an isolated ephemeral project.
6. As the runner account, run `runner preflight`; as the GitHub/Jenkins machine
   user, perform the documented handoff preflight without printing its token.
7. Start the runner and execute one reviewed protected-main Fixture through the
   GitHub workflow. Record only its exact run and artifact URLs in the readiness
   checklist.

The local smoke is not a substitute for the protected-main run. Reconstruction
does not create host accounts, users, tokens, branch rules, environment rules,
or firewall policy.

## Maintenance And Rotation

Maintain one runner, Domain agent, or credential at a time while no run is queued
or building. Use the order provision, validate, run one immediate protected-main
Fixture, then revoke the predecessor. `reconcile-agent` enters Jenkins quiet
mode, refuses replacement unless the fixed queue and job are idle, and resumes
Jenkins after replacement or failure. Stop the GitHub runner first so no handoff
is stranded in GitHub while Jenkins is quiet. A descriptor replacement is
valid only by changing that descriptor's immutable image digest and then
reconciling its one matching agent.

For administrator passwords and the handoff machine-user token, stage one
successor outside the checkout, validate its least privilege and intended
identity, prove the live Fixture, and only then revoke or remove its predecessor.
Jenkins derives all five inbound registration secrets from one controller HMAC
key, so they cannot truthfully be rotated independently. Replace agent
containers one at a time with `reconcile-agent`; replace the shared registration
key only during stopped reconstruction or the disposable recovery procedure,
which proves all five successors before the target uses them. Never place values
in commands, logs, manifests, readiness evidence, or repository files. No
automatic upgrade or periodic rotation machinery is provided.

## Manual Security Review

Before readiness, manually confirm protected-main-only dispatch, `contents: read`
workflow permission, protected-environment secret delivery, trusted collaborator
and fork policy, loopback-only Jenkins publication, a least-privilege handoff
machine user, separate administrator and agent identities, a low-privilege
runner without Docker or Jenkins-state access, Stage-scoped secret bindings, and
sanitized evidence. Use `docs/installation-readiness-checklist.md`; do not paste
Jenkins console output as evidence.

## Troubleshooting

- Preflight failures: fix the named host capability as an administrator, then
  rerun preflight. Do not weaken a check or add privilege escalation.
- Offline runner: verify the user service and GitHub repository runner status;
  an offline runner leaves workflow work queued and does not justify bypassing
  the protected workflow.
- Offline agent: check only the affected container and secret file, then use
  one-at-a-time reconciliation after Jenkins is idle.
- JCasC or Job DSL drift: stop new handoffs, restart from the approved commit,
  and run `verify-live`; do not preserve UI drift as configuration.
- Recovery mismatch: retain both files, reject the restore, and investigate the
  approved commit, pin authority, size, and digest without opening or copying
  sensitive contents into an issue.
- Partial restore: keep Jenkins stopped, destroy only the failed empty-state
  project volume, and retry from the verified archive.

## Verification

Run the empty-state runtime acceptance check with:

```sh acceptance=stack-smoke
deployment/jenkins/bin/stack smoke
```

The smoke check uses an isolated Compose project, owner-only temporary bootstrap
files, five distinct temporary registration secrets, a random loopback port,
and a fresh named volume. It verifies the exact Jenkins, plugin, Remoting,
Python, and `uv` inputs; native JCasC validation; controller and agent executor
counts; labels and identities; WebSocket connectivity; the fixed job; and the
handoff authorization boundary. Temporary jobs clean before and after each
allocation and execute twice on every label to prove correct node allocation,
clean workspaces, the integration/Domain role boundary, one-secret visibility,
missing Docker access, and runtime network isolation. The checker also
distinguishes missing labels, invalid mappings, incorrect executor counts,
unavailable agents, and admissible busy agents.

The smoke then mutates controller and node configuration, deletes one managed
node and the fixed job, restarts all six containers while preserving the
controller volume, and proves JCasC and Job DSL restore repository state. Agent
tmpfs workspaces are recreated empty. Cleanup removes the isolated smoke volume.

`integration/scripts/verify` includes this smoke check after the Python package
checks. The smoke also replaces the controller passwords and Jenkins inbound
agent HMAC key, proves all five newly issued registration secrets differ, and
reconnects every agent. It therefore requires the supported Docker host and
network access when the controller image is not already cached.

## Pin Maintenance

The selected controller is the current weekly-reviewed Jenkins LTS available for
this implementation and uses the official immutable Linux/amd64 image manifest.
The plugin closure was resolved with the controller image's maintained Jenkins
Plugin Installation Manager using `--latest=false`; startup and the smoke check
prove that the runtime set equals the lock exactly. Jenkins core provides the
global simple build discarder, so no extra build-discarder plugin is needed.

Review Jenkins security advisories, plugin health and licenses, minimum-core
requirements, and upstream release notes before changing any pin. Refresh the
complete closure as one review unit, run the empty-state smoke check, and retain
the previous image reference for operational rollback. Primary maintenance
sources are the Jenkins LTS changelog, official Jenkins Docker image, Jenkins
plugin index, Configuration as Code plugin, Job DSL plugin, and Matrix
Authorization plugin.

The agent build combines the descriptor-selected CPython 3.12.13 Fixture runtime
with the official Jenkins inbound-agent `3391.va_37fa_a_305d6d-2` JDK 21 image
and official `uv` 0.12.1 image. The production integration package and its exact
dependencies are installed from `integration/uv.lock` while the image has build
network access, so agents need no runtime package download.
All three image inputs are immutable multi-platform digests, and the deployment
selects Linux/amd64. Remoting files are copied into the descriptor runtime rather
than inherited as the final image, preventing the upstream anonymous volume
declarations from persisting agent state. The custom entrypoint exists only to
read the externally mounted registration file without placing its value in
Compose environment configuration.

Controller health and Fixture execution prove pipeline machinery only. They are
not Domain success, Validation evidence, deployment evidence, or KPI evidence.

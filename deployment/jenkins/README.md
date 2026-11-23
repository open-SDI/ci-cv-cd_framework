# Jenkins Deployment

This directory owns the reconstructable Jenkins control plane and five inbound
agents. The controller runs on supported Ubuntu LTS x86_64, has no executors,
and is started only on demand. One dedicated `integration` agent performs only
integration work; four independently replaceable agents provide the
`composition`, `image-build`, `cv`, and `cd` scheduling labels. The root pipeline
executes one submitted run across those five agents: implemented composition,
then the image-build, CV, and CD Fixtures. A BuildKit builder and a pipeline
registry, reachable only from the `image-build` agent, are ready for the real
image-build adapter. Optional GitHub self-hosted runner
services, one per repository, pick up the protected handoff job, reach the
controller over an internal network, and reach GitHub only through their own
allowlisting proxy.

The tracked authority is:

- `controller/Dockerfile`: Jenkins `2.568.3` on JDK 21, pinned to the reviewed
  Linux/amd64 image digest.
- `controller/plugins.txt`: the complete 47-plugin runtime set, including every
  transitive dependency at an exact version.
- `agents/Dockerfile`: the exact Jenkins Remoting, Python 3.12.13, `uv`, and
  locked production integration package used to construct each minimal agent
  image without inherited volume metadata. Its `image-build` target adds the
  `buildctl` client copied from the pinned BuildKit image.
- `buildkit/buildkitd.toml`: the read-only builder configuration, see
  [Builder And Pipeline Registry](#builder-and-pipeline-registry).
- `runner/Dockerfile`: the GitHub Actions runner `2.337.0` image for the
  handoff runners, with the same Python runtime and locked production
  integration package as the agents.
- `runner-proxy/Dockerfile` and `runner-proxy/squid.conf`: the Squid 6 egress
  proxy for each handoff runner, built on the same pinned Python runtime image
  from Debian's `squid` package, and its GitHub Actions endpoint allowlist.
- `casc/jenkins.yaml`: global security, zero-executor, five-node, retention, and
  Job DSL configuration.
- `jobs/pipeline-integration.groovy`: the two fixed parameterized Pipeline SCM
  jobs, `pipeline-integration` for upstream and `pipeline-integration-fork` for
  the fork.
- `compose.yaml`: loopback-only controller publication, named controller state,
  five agent services, a separate egress-capable network for each agent, the
  `buildkitd` builder and `registry` services with their named volumes, the
  two optional runner services `runner-upstream` and `runner-fork`, and each
  runner's proxy service `runner-upstream-proxy` or `runner-fork-proxy`.
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
- Docker Engine 27 or newer with the Docker Compose plugin 2.20.2 or newer,
  which the healthcheck `start_interval` key requires, usable by the operator
  without privilege escalation.
- Python 3 for the isolated smoke-check client.
- `uv` for strict adapter-descriptor validation and runtime-image selection.
- Outbound HTTPS access to Docker Hub, GHCR, the Jenkins update service during a
  clean image build, PyPI during a clean agent or runner build, and GitHub for
  the runner release download and each agent's independent immutable checkout.

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
`config/local/controller.env` and set every value. `JENKINS_REPOSITORY_URL`
names the upstream repository and `JENKINS_FORK_REPOSITORY_URL` the fork; each
must be a distinct uncredentialed HTTPS GitHub clone URL.
`JENKINS_AGENT_CONTROLLER_URL` is exactly `http://controller:8080` for local
Compose agents and an uncredentialed HTTPS URL for a relocated agent. Persisted
container IPs and machine names are not configuration. The host port, user IDs,
endpoint, repository identity, and secret locations are local deployment
settings rather than tracked topology. The run and Stage work limits are not
settings; they come from the Stage registry,
`integration/src/sdi_pipeline_integration/_stage_registry.py`, and `bin/stack`
refuses a configuration that still sets one. The registry is the source of Stage
wiring. JCasC, `compose.yaml`, `bin/smoke-check`, and `bin/stack` keep
hand-kept copies of some registry facts; update each copy to match a registry
change. `integration/tests/test_stage_wiring.py` fails until they match. Regenerate
the `Jenkinsfile` Stage block from the repository root with
`uv run --project integration sdi-integration jenkinsfile --write --jenkinsfile Jenkinsfile`.

Create the administrator and machine-user password files and the OpenAI API key
file outside the checkout. Each file must be nonempty, owned by the operator, and
inaccessible to group and other users, for example mode `0600`. Compose mounts
them into the controller only, through `/run/secrets`; secret values are never
placed in tracked files or Compose environment values.

JCasC creates the two local identities from those bootstrap files. The
administrator has `Overall/Administer`. The handoff machine user has only
`Overall/Read` globally and `Job/Read`, `Job/Build`, and `Job/Cancel` on both
fixed jobs. Anonymous users receive no permissions. The administrator must later
issue the named machine-user API token and place it only in the protected
`pipeline-integration-jenkins` GitHub environment. Password authentication is
not the GitHub-to-Jenkins contract.

Each dedicated GitHub runner is a stack service, described in
[GitHub Handoff Runners](#github-handoff-runners). It runs as a non-root user
with no Docker socket, Jenkins state, or secret files. The container receives
no environment and never persists this API token; GitHub releases it only to
the protected handoff job.

Jenkins serves two repositories through two otherwise identical jobs:
`pipeline-integration` clones upstream and `pipeline-integration-fork` clones
the fork. Fork-specific names carry the `-fork` suffix. The `Jenkinsfile`
selects the repository by its job name and refuses any other job, so the
protected-main ancestry check runs against the repository that submitted the
run. Each repository has its own runner service, `runner-upstream` for upstream
and `runner-fork` for the fork, and its own
`pipeline-integration-jenkins` GitHub environment whose `SDI_JENKINS_JOB_PATH`
names its job. Both jobs share the five agents, each with one executor, so
overlapping runs queue or time out.

The agent and runner images bake the integration code of the checkout that ran
`stack start`, normally fork `main`, while each run executes the workflow,
handoff, and `Jenkinsfile` of its own repository. Dispatch on upstream only
after a sync pull request has brought upstream `main` level with the fork
`main` commit the images were built from. A run whose integration code differs
from the baked code fails with a typed reason before any handoff or Stage work,
even when the baked CLI does not accept this `Jenkinsfile`'s flags, as described
below.

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
agent receives no Domain credential and has no Domain label. The three Fixture
descriptors declare no Domain secret binding, and no agent container sees a
controller password.

The composition descriptor binds `OPENAI_API_KEY` and `VLLM_KEY`. The root
pipeline names no composition config or generator, so it composes with the
default luna generator of the committed `integration/composition-config.yaml`;
the agents cannot reach the self-hosted qwen endpoint. It binds the Jenkins secret
text credential `sdi-openai-api-key` as `OPENAI_API_KEY` around the composition
Stage only. JCasC creates that credential from the file named by
`JENKINS_OPENAI_API_KEY_FILE`, which holds only the key. JCasC reads that file
only at controller startup and overwrites UI edits to the credential, so rotate
the key while no build runs: rewrite the file, keep it mode `0600`, then run
`bin/stack stop` and `bin/stack start`. Allow the `ci` agent
HTTPS egress to `api.openai.com`, which serves both luna and the embeddings.
Without a valid key or that egress, composition fails with
`sdi.composition.dense-discovery-unavailable` and the build is red.
The composition and image-build Stage commands read `service-repository/` from
their own checkout of the submitted commit, because the Stage registry grants
it to them, and the image-build command refuses to run unless its capture
matches the one composition accepted. The CV and CD Stage commands, preflight,
and finalization take no service repository.

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

`start` pulls the pinned builder and registry images when they are missing,
builds the exact
controller and five agent images and the runner and proxy images of every
runner configured in `GITHUB_RUNNERS`, starts the controller, all five agents,
the builder, the registry, and those runners with their proxies, rejects an
agent image that declares any volume, and waits for process health. It refuses
a configured runner that is not registered and refuses while a runner executes
a job. Jenkins is published on the host only
at
`http://127.0.0.1:<configured-port>`. No inbound-agent TCP port is published;
all agents connect over WebSocket. `stop` removes all containers and networks
but retains every named volume: `jenkins-home`, the runner state volumes, and
the builder's `buildkit-state` and `pipeline-registry`. Agent workspaces are
separate tmpfs mounts, have no shared or persistent filesystem, and disappear
whenever their container is replaced. Their five separate networks allow GitHub
checkout without making peer agents reachable. `stop`, `status`, `logs`, and `destroy`
include the runner and proxy containers. Every image the stack builds carries
the label `org.sdi.project=ci-cv-cd_framework`.

`build` validates the configuration, builds the controller and five agent
images, rejects an agent image that declares any volume, and writes a
`build-stamp` file beside the configuration file. The stamp records the tracked
checkout, including uncommitted changes, and the IDs of the built images.
`--prebuilt` reuses those images, as in `bin/stack --prebuilt start`. It applies
only to `start-controller`, `start`, `verify-live`, and `exercise-live`, which then
skip configuration validation and the controller and agent image builds. Each
refuses to run when the stamp does not match the current checkout and images;
run `build` again. Runner and proxy images are still built.

### Builder And Pipeline Registry

The always-on stack has eight containers: the controller, the five agents,
`buildkitd`, and `registry`. Both images are pinned by digest in
`deployment/installation.env` as `SDI_BUILDKIT_IMAGE` and `SDI_REGISTRY_IMAGE`.

- `buildkitd` runs privileged, listens on `tcp://buildkitd:1234` without TLS,
  and reads `buildkit/buildkitd.toml`. It grants no insecure entitlements. Its
  cache is cleaned by one space-only rule, with no time-based expiry.
- `registry` serves plain HTTP on `registry:5000`. It has a read-only root
  filesystem, no capabilities, and no published port, so nothing outside the
  stack pulls from it.
- The internal `image-build-builder` network joins only the `image-build`
  agent, `buildkitd`, and `registry`; network membership is the access
  boundary. Only `buildkitd` also joins `builder-egress`, which is unrestricted
  so recipes can fetch packages. The `image-build` agent keeps the same
  checkout egress as every agent and is the only image with `buildctl`.
- Builder cache and registry storage live in the `buildkit-state` and
  `pipeline-registry` named volumes. They survive `stop` and `start`;
  `destroy` removes them and is the reset. Recovery snapshots do not include
  them.

Building for `linux/arm64` relies on QEMU emulation registered with the host
kernel's binfmt_misc. Docker Desktop registers it; on a plain Docker Engine
host, register the arm64 emulator before `start`, or the smoke check fails
because `buildkitd` lists no `linux/arm64` platform.

### GitHub Handoff Runners

The protected handoff job runs on a dedicated self-hosted GitHub runner with the
sole label `sdi-jenkins-handoff`. Each repository's runner is an optional stack
service: `runner-upstream` for `JENKINS_REPOSITORY_URL` and `runner-fork` for
`JENKINS_FORK_REPOSITORY_URL`. The optional `GITHUB_RUNNERS` key in
`config/local/controller.env` selects which ones `start` builds and starts. It
is empty by default, or `upstream`, `fork`, or `upstream,fork`.

Register a configured runner once, with the stack running, by piping a fresh
registration token into `register-runner`. The token is a one-hour GitHub token
that only a repository administrator can create:

```sh acceptance=stack-register-runner
gh api -X POST repos/open-SDI/ci-cv-cd_framework/actions/runners/registration-token \
  --jq .token | deployment/jenkins/bin/stack register-runner upstream
deployment/jenkins/bin/stack start
deployment/jenkins/bin/stack verify-live
```

For the fork, request the token from
`repos/gurkhaman/ci-cv-cd_framework/actions/runners/registration-token` and run
`register-runner fork`. The command requires the runner in `GITHUB_RUNNERS`,
refuses a token typed at a terminal, refuses while that runner's container
runs, builds the runner and proxy images, starts the runner's proxy, and runs
the runner network check described below. After it accepts the token's form,
the runner confirms that its proxy opens tunnels to the GitHub endpoints it
uses, then registers. A failed registration leaves the proxy running; `stop` or
`destroy` removes it. It registers against the configured repository URL
without `.git` and prints the repository URL and runner name, never the token.
The default names are `jenkins-handoff-stack` and `jenkins-handoff-stack-fork`;
another installation passes its own with `--name NAME`. An existing registration
is refused unless `--replace` is given, which discards the local registration
and replaces a same-named runner in GitHub. `start` refuses a configured runner
that is not registered and prints the exact registration command.

GitHub assigns a queued job to any online runner with the label, so only one
installation may have an online upstream runner at a time. When another
installation's upstream runner goes live, stop this one by setting
`GITHUB_RUNNERS=fork` and restarting the stack.

Each runner image installs the Actions runner release, verified against its
pinned SHA-256, with automatic updates and default labels disabled. It bakes the
integration environment at `/opt/sdi/integration/.venv` and its code digest at
`/etc/sdi/integration-code-digest`, so handoff jobs download and compile
nothing. The protected workflow fails a job whose checkout's integration digest
differs from the runner's; rebuild with `stack start` from a clean checkout of
that revision. The container runs as the non-root `runner` user without sudo,
with a read-only root, all capabilities dropped, `no-new-privileges`, no
environment except its proxy settings, no secrets, Docker socket, or published
ports, and no automatic restart. Its job workspace and `/tmp` are tmpfs mounts,
so every checkout, including its sparse-checkout patterns, is discarded whenever
the container restarts. The named volume `runner-<repo>-state` holds the runner
binaries, which the entrypoint replaces from the image on every start, and the
registration. The registration files `.runner`, `.credentials`, and
`.credentials_rsaparams` are long-lived credentials of that runner.

The runner joins only the internal network `runner-<repo>-controller`, shared
with the controller and its proxy, so it has no default route and resolves no
external name. The missing external DNS relies on Docker Engine 27 or newer,
which the stack preflight requires, not forwarding external lookups for
containers on internal-only networks. It reaches Jenkins directly as
`http://controller:8080`. Its `HTTPS_PROXY` and `HTTP_PROXY` name
`http://runner-<repo>-proxy:3128`, and `NO_PROXY` exempts only `controller`,
`localhost`, and `127.0.0.1`. The proxy alone joins `runner-<repo>-egress`. It
admits only HTTPS `CONNECT` tunnels on port 443 to `github.com`,
`api.github.com`, `codeload.github.com`, any `*.actions.githubusercontent.com`
host, and any `*.blob.core.windows.net` host, which serves Actions artifacts and
logs. It refuses IP-literal targets, plain HTTP, and every other host with a
Squid `403`. It caches nothing, runs as the non-root `proxy` user with the same
container hardening as the runner, keeps its runtime files on tmpfs, and logs
each request to the container log. `objects.githubusercontent.com` is not
admitted because it serves only runner self-updates, which the runner disables.
When GitHub changes its Actions endpoints, update the allowlist in
`runner-proxy/squid.conf` and the hosts the runner's `egress` check probes in
`runner/sdi-runner-entrypoint` together.

`sdi-runner-entrypoint check` proves, from inside the runner, that the
controller answers directly; that the proxy refuses `example.com`, IPv4 and
IPv6 literal tunnels, and plain HTTP with a Squid `403`; that the container has
no default route; and that `example.com` does not resolve while `controller`
and the proxy do. It needs no internet access. `sdi-runner-entrypoint egress`
proves the proxy opens a tunnel to each concrete GitHub endpoint the runner
uses and therefore needs internet access. `verify-live` runs both in every
running configured runner.

To remove a runner, stop the stack, remove it from `GITHUB_RUNNERS`, delete the
runner in the repository's Actions runner settings, and delete its registration
with `docker volume rm <project>_runner-<repo>-state`, for example
`sdi-jenkins_runner-upstream-state`. `destroy` also deletes both runner state
volumes; the GitHub-side runner then stays offline until it is deleted in GitHub
or re-registered with `--replace`.

Agent images install the integration package when they are built, so a
pipeline run checks out the submitted commit but runs the adapter code baked
into its agent image. Runner images bake it the same way. Each image records a digest of the integration
`pyproject.toml`, `uv.lock`, `src`, and `fixtures` it bakes in
`/etc/sdi/integration-code-digest`. Before any Stage work, the run compares
every agent's digest with its checkout's, and each Stage agent repeats the
comparison for itself before its Stage. A mismatch fails the Jenkins build with
the reason `sdi.agent.stale-image`, naming the stale agents, and the handoff
receipt records the failure code `stale_agent_image`. Changes elsewhere, such
as docs, tests, and schemas, do not count; any change to the four baked paths
does, including dev-only dependency or tool settings.

So after merging a change to the baked paths, shut the stack down as in
[Safe Shutdown](#safe-shutdown) and run `stack start`, which rebuilds the agent
and runner images; the guard fails every run until you do. Images built before the guard
existed record no digest, so the first run on them fails with the same typed
reason.
The runner image records the same digest, and the protected workflow's
"Confirm runner integration code matches the revision" step fails the handoff
job before contacting Jenkins when it differs from the checkout's.
`stack exercise-live` and the `verify` smoke check assert that each agent
records the digest of the working tree that built it. The image build copies
the working tree, not the commit, so run `stack start` from a clean checkout:
an uncommitted or untracked file under the four baked paths bakes a digest that
no submitted commit matches, and every run then fails as stale.

The root pipeline deletes every allocated workspace before checkout and in a
`finally` block. Each agent independently checks out and verifies the submitted
protected-main commit. The checkout is a blobless partial clone with a sparse
checkout that omits the unused `CV/` Gazebo assets, nearly all of the
repository's bytes; full commit history is kept for the ancestry check. The
controller's Pipeline-script checkout keeps its repository between builds and
fetches only new commits. Accepted envelopes and their declared bounded files are
the only cross-agent Stage data, transferred through Execution-ID-qualified
Jenkins stashes. The integration agent validates the complete bundle before
archiving all and only its declared files from the current build.

The pipeline uses the Stage registry's 10, 30, 45, and 15 minute Stage work
limits, a 90-minute Jenkins run limit, and a two-minute finalization limit. A
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
removes the controller, its named volume, the runner state volumes with their
registrations, and the builder cache and pipeline registry volumes. Back up
operational state first if it must be retained.

## Safe Shutdown

Prevent new protected-environment delivery before shutdown. Then quiesce the
runners and Jenkins and stop the stack:

```sh acceptance=stack-safe-shutdown
deployment/jenkins/bin/stack quiesce
deployment/jenkins/bin/stack stop
```

`quiesce` refuses while any runner executes a job and stops the running runner
containers, giving each 60 seconds. It then asks Jenkins to stop accepting new
work and refuses while the fixed job has queued or building work. If it
observes active work, it cancels quiet mode, restarts the runners, and leaves
the stack running. A job that GitHub assigns between the runner check and the
stop is cancelled gracefully by the stop, and its workflow run shows cancelled.
Do not use `stop` directly during an active run. `stop` gives every container
60 seconds to terminate, rejects a forced SIGKILL shutdown, and only then
removes the containers. Resume an intentionally quiesced live controller and
its configured runners with `stack resume`; after a full shutdown, `stack start`
starts the stack and its configured runners.

## Backup And Restore

Snapshots are manual, cold, and sensitive. Select an operator-encrypted
destination outside both the checkout and Docker volume, stop the
protected-environment handoff, and run:

```sh acceptance=recovery-snapshot
deployment/jenkins/bin/recovery snapshot \
  --destination /operator/encrypted/off-host-location \
  --encrypted-destination \
  --handoff-quiesced
```

The acknowledgement flags record operator decisions; the scaffold cannot prove
storage encryption or GitHub-side quiescence. The command then runs
`stack quiesce`, which stops the runners and quiesces Jenkins, refuses queued
or building runs, cleanly stops every container, and archives the complete
`jenkins-home` named volume with numeric ownership. The runner state volumes,
and so the runner registrations, and the builder cache and pipeline registry
volumes are not part of the snapshot. It creates an
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

Restore only from the exact approved commit recorded in the manifest, with
`GITHUB_RUNNERS` empty so no runner starts during restoration. Stage new
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
target. Before configuring the runners in `GITHUB_RUNNERS` again or restoring
protected-environment handoff, the administrator must issue a successor token
for the handoff machine user, replace `SDI_JENKINS_API_TOKEN` in the protected
`pipeline-integration-jenkins` environment, and perform the documented
machine-user preflight without exposing the token. Then register each runner as
in [GitHub Handoff Runners](#github-handoff-runners), with `--replace` when
its state volume or the GitHub-side runner still holds an old registration. A failed target extraction leaves a partial volume that must be
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
   installation. These prove the zero-executor controller, both fixed jobs, five
   identities and labels, one executor per agent, clean isolated workspaces,
   exact pins, and convergence. Run `stack smoke` separately for repository
   acceptance; it uses an isolated ephemeral project.
6. As the GitHub/Jenkins machine user, perform the documented handoff preflight
   without printing its token. Set `GITHUB_RUNNERS` and register each runner
   with `stack register-runner`, using `--replace` because `destroy` deleted
   the old registrations.
7. Run `stack start` and `stack verify-live`, then execute one reviewed
   protected-main Fixture through the GitHub workflow. Record only its exact run
   and artifact URLs in the readiness checklist.

The local smoke is not a substitute for the protected-main run. Reconstruction
does not create host accounts, users, tokens, branch rules, environment rules,
or firewall policy.

## Maintenance And Rotation

Maintain one runner, Domain agent, or credential at a time while no run is
queued or building. Use the order provision, validate, run one immediate
protected-main Fixture, then revoke the predecessor. `reconcile-agent` enters
Jenkins quiet mode, refuses replacement unless the queue and both fixed jobs are
idle, and resumes Jenkins after replacement or failure. Run `stack quiesce`
first, which stops the GitHub runners so no handoff is stranded in GitHub while
Jenkins is quiet, and `stack resume` afterwards to restart them. A descriptor
replacement is valid only by changing that descriptor's immutable image digest
and then reconciling its one matching agent.

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
machine user, separate administrator and agent identities, hardened runner
containers, Stage-scoped secret bindings, and sanitized evidence. Use
`docs/installation-readiness-checklist.md`; do not paste Jenkins console output
as evidence.

Each runner reaches the controller directly over its own internal network and
the internet only through its own proxy, which admits HTTPS tunnels to the
GitHub Actions endpoints alone. It has no Docker socket, no Jenkins state or
secret files, and no Jenkins credential except the API token GitHub releases to
the protected handoff job. The proxy stops job code from reaching arbitrary
hosts, the host's LAN, and cloud metadata services; it does not prevent data
exfiltration. The residual risk is that code from any workflow that targets
`sdi-jenkins-handoff` in that repository can still send data to any Azure
storage account under `*.blob.core.windows.net`, or to `github.com` and
`api.github.com` with credentials an attacker supplies, and can reach the
Jenkins HTTP API by design, though it still needs credentials it does not
otherwise have. The proxy does not inspect TLS or filter paths, and it does not
chain to an upstream corporate proxy. The label remains an operational
selector, not an authorization control.

## Troubleshooting

- Preflight failures: fix the named host capability as an administrator, then
  rerun preflight. Do not weaken a check or add privilege escalation.
- Offline runner: check `stack status`, `stack logs`, and the repository's
  Actions runner list; an offline runner leaves workflow work queued and does
  not justify bypassing the protected workflow.
- Unregistered runner: `start` prints the exact registration command; pipe a
  fresh token into `stack register-runner`, with `--replace` when GitHub still
  lists a same-named runner.
- Runner digest mismatch: the "Confirm runner integration code matches the
  revision" step failed; run `stack start` from a clean checkout of that
  revision to rebuild the runner image.
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
files, five distinct temporary registration secrets, a random loopback port, and
a fresh named volume. It verifies the exact Jenkins, plugin, Remoting, Python,
and `uv` inputs; native JCasC validation; controller and agent executor counts;
labels and identities; WebSocket connectivity; both fixed jobs; and the handoff
authorization boundary. Temporary jobs clean before and after each allocation
and execute twice on every label to prove correct node allocation, clean
workspaces, the integration/Domain role boundary, one-secret visibility, missing
Docker access, and runtime network isolation. Only the `image-build` agent
resolves `buildkitd` and `registry` and has `buildctl`, at the pinned BuildKit
version. On its first allocation it confirms that `buildkitd` lists
`linux/arm64`, builds a `FROM scratch` image for `linux/amd64` and
`linux/arm64`, pushes it to the registry, and reads back the index and both
platform manifests by digest. The checker also distinguishes
missing labels, invalid mappings, incorrect executor counts, unavailable agents,
and admissible busy agents. The smoke builds the runner and proxy images and,
without contacting GitHub, proves that registration runs the runner network
check, so the runner reaches the controller directly, the proxy refuses
non-allowlisted, IP-literal, and plain HTTP requests, and the runner has no
default route or external DNS, then rejects an invalid token; and that `start`
refuses the unregistered runner. It never registers a runner and does not run
the internet-dependent `egress` check.

The smoke then mutates controller and node configuration, deletes one managed
node and both fixed jobs, restarts the controller and all five agents while
preserving the controller volume, and proves JCasC and Job DSL restore
repository state. Agent tmpfs workspaces are recreated empty. Cleanup removes
the isolated smoke volumes and prunes dangling images labelled
`org.sdi.project=ci-cv-cd_framework`, such as superseded builds of the fixed
image tags. Unlabelled images, base images,
and the build cache are untouched.

`integration/scripts/verify` includes this smoke check after the Python package
checks. The smoke also replaces the controller passwords and Jenkins inbound
agent HMAC key, proves all five newly issued registration secrets differ, and
reconnects every agent. It therefore requires the supported Docker host,
arm64 emulation, and network access when the controller, builder, and registry
images are not already cached.

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

The agent build combines the descriptor-selected CPython 3.12.13 runtime
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

The runner image uses the same CPython 3.12.13 runtime and `uv` inputs and
shares the agents' integration build stage. It downloads Actions runner
`2.337.0` with a Docker `ADD --checksum` SHA-256 check, so the build fails on a
mismatch, and asserts that the installed listener reports the pinned version.
`SDI_GITHUB_RUNNER_VERSION` and `SDI_GITHUB_RUNNER_SHA256` in
`deployment/installation.env` must match the `RUNNER_VERSION` and
`RUNNER_SHA256` arguments in `runner/Dockerfile` and the runner image tag in
`compose.yaml`, and `SDI_GITHUB_RUNNER_LABEL` must match the label in
`runner/sdi-runner-entrypoint`; `validate-installation` checks them. Change a
runner pin in all of those places as one review unit. Automatic updates are
disabled, and GitHub stops assigning jobs to a runner that falls too far behind
its current release, so repin the runner promptly after each Actions runner
release rather than treating the bump as optional. The Debian `git` and
`libicu76` packages are installed with `apt-get` during the build and are not
version-pinned.

The runner proxy image builds on the same pinned CPython runtime image, so it
has no `installation.env` key of its own; `validate-installation` checks that
`runner-proxy/Dockerfile` consumes `SDI_PYTHON_RUNTIME_IMAGE` and that both
proxy services in `compose.yaml` are tagged with its `SQUID_MAJOR` argument.
The Debian `squid` package is installed with `apt-get` during the build and is
not version-pinned; the build asserts that the installed Squid has the
`SQUID_MAJOR` major version. Change `SQUID_MAJOR` and both proxy image tags as
one review unit; a runtime pin change rebuilds the proxy with the runner.

`SDI_BUILDKIT_IMAGE` must match the `BUILDKIT_IMAGE` argument in
`agents/Dockerfile` and the `buildkitd` image in `compose.yaml`, and
`SDI_REGISTRY_IMAGE` must match the `registry` image there;
`validate-installation` checks them. The `image-build` agent's `buildctl` is
copied from that BuildKit image, so client and daemon match by construction.
The smoke check asserts the client version through `EXPECTED_BUILDCTL_VERSION`
in `bin/smoke-check`. Change the BuildKit pin in all of those places as one
review unit, after reviewing the BuildKit and distribution release notes and
security advisories.

Controller health and Fixture execution prove pipeline machinery only. They are
not Domain success, Validation evidence, deployment evidence, or KPI evidence.
The descriptor `image` selects each agent's runtime base; the runtime starts the
descriptor's entry point from the agent's installed package rather than pulling
that image per Stage.

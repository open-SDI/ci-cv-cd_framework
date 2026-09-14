# Pipeline Integration Scaffold Maintainer Guide

## Contract Authorities

The supported interfaces are the committed file contracts, generated JSON
Schemas, `uv run --project integration sdi-integration`, the Stage-adapter
process contract, `deployment/jenkins/bin/stack`,
`deployment/github-runner/bin/runner`, and
`deployment/jenkins/bin/recovery`. Internal Python modules, Jenkins UI state,
container IP addresses, mutable image tags, and local configuration are not
compatibility contracts.

Authority is intentionally local:

- `deployment/installation.env` owns supported host releases, minimum tool
  majors, required public HTTPS destinations, and exact installation pins.
- `integration/src/sdi_pipeline_integration/` owns validation semantics and
  generates `integration/schemas/`; committed schemas are language-neutral
  consumer contracts.
- `integration/stage-profiles/` owns Stage invocation policy and limits.
- `deployment/jenkins/adapters/` owns which immutable implementation image and
  process satisfy each Stage seam.
- `deployment/jenkins/casc/jenkins.yaml`, `jobs/`, `compose.yaml`, and the root
  `Jenkinsfile` own reconstructable scheduling and security configuration.
- `.github/workflows/pipeline-integration.yml` owns the protected external
  trigger and publication experience.
- Pipeline integration results and handoff receipts own run facts. Jenkins
  presentation and a future dashboard must not invent another verdict.

Runtime Jenkins state holds history and generated controller material, but
tracked configuration wins on restart. Recovery archives are sensitive runtime
state, not source authority.

## Ownership Seams

Pipeline integration owns committed-input identification, one Execution ID,
Stage ordering, process invocation, bounded transfer, deadlines, cancellation,
result assembly, schema validation, GitHub/Jenkins correlation, and publication
semantics. Jenkins owns private scheduling and transient build state. GitHub owns
protected dispatch, environment delivery, run presentation, and retained
artifacts. Host administrators own the OS, Docker, accounts, firewall, SSH,
storage encryption, and credential decisions.

Each Domain team owns its adapter implementation and Domain output semantics.
The integration scaffold validates an adapter only at the descriptor-selected
process seam. It does not absorb Domain logic or infer a Domain or KPI verdict.

## Independent Versioning

Schema versions, run-request versions, Stage-profile versions, adapter descriptor
versions, handoff versions, recovery-manifest versions, and implementation image
versions evolve independently. Change only the authority that owns the semantic
change. A compatible adapter implementation update does not require a schema
version change. A contract meaning or shape change does.

Never reuse a version identifier for different semantics. Regenerate schemas
after model changes and review generated diffs. Retain exact digests and lock
files; tags alone are not pins.

## Adapter Replacement

Replace a Domain adapter only by reviewing its descriptor and immutable image
digest. Do not add Stage-specific branches to the Jenkinsfile, copy Domain logic
into integration code, or mutate a running container. With Jenkins idle:

1. Review the image provenance, license, vulnerability posture, process command,
   declared inputs/outputs, secret bindings, and supported architecture.
2. Commit the one descriptor and any independently versioned contract change.
3. Run `integration/scripts/verify`.
4. Run `stack reconcile-agent <ci|image-build|cv|cd>` for only that descriptor's
   agent.
5. Execute one protected-main Fixture, then the adapter's real acceptance when
   that Domain implementation exists.
6. Revoke or remove the predecessor only after validation.

## Verification And Readiness

`integration/scripts/verify` is the deterministic repository acceptance command.
It checks generated-schema freshness, formatting, linting, type safety, tests,
and an ephemeral six-container Jenkins smoke. It does not prove GitHub
protection, protected-environment delivery, the installed runner, production
credentials, restoration, or a live protected-main run.

Repeat the relevant readiness sections after any change to installation pins,
workflow permissions, branch/environment protection, runner, controller,
plugins, JCasC, Job DSL, Compose topology, agent image, descriptor, credential,
recovery procedure, or host security posture. Repeat full reconstruction and
restoration evidence before declaring a new installation ready. Use
`docs/installation-readiness-checklist.md` and retain links or sanitized facts,
not copied console output.

## Deferred Domain Capabilities

The current four-Stage chain is explicitly a Fixture. The following remain
deferred and must not be claimed, inferred, or quietly added as maintenance:

- real mobility-requirement analysis, service discovery, compatibility checking,
  composition, and placement selection;
- real multi-architecture image builds and supply-chain publication;
- real Gazebo or Isaac Sim scenario execution, oracles, measurements, Coverage
  Gap selection, Validation evidence, and regression verdicts;
- real distributed CD placement, deployment, rollout, health checking, rollback,
  state migration, and degraded-network behavior;
- an agreed KPI-7 formula or proof of any project KPI;
- a production result store, dashboard, service catalog, performance database,
  runtime monitoring, anomaly detection, recomposition, autoscaling, or adaptive
  replacement;
- Kubernetes integration and physical mobility-device confirmation;
- backup scheduling, automatic recovery, a maintenance database, high
  availability, availability targets, RPO, RTO, broad infrastructure as code,
  or automated security/account administration.

Open a dedicated implementation issue with an authoritative contract before
introducing any deferred capability.

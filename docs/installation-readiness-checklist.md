# Installation Readiness Checklist

Use one copy of this checklist per installation acceptance or required
repetition. Record role-based confirmation, UTC time, approved commit, exact run
or artifact URLs, and concise pass/fail facts only.

Never record credentials, tokens, secret values, private endpoints,
machine-specific secret paths, copied Jenkins console output, recovery archive
contents, hostnames, account names, or personal information. Store snapshots and
manifests only in the approved operator-encrypted location outside the repository
and Jenkins volume.

## Scope

- [ ] Review reason or change set:
- [ ] Approved protected-main commit:
- [ ] Checklist started at (UTC):
- [ ] Accountable roles represented: maintainer / host administrator / operator
- [ ] No prohibited evidence is present

## Repository

- [ ] `integration/scripts/verify` passed at the approved commit
- [ ] Generated schemas are current, including the recovery manifest
- [ ] `deployment/installation.env` matches all tracked pins
- [ ] Fixture limitations and deferred Domain capabilities were acknowledged
- [ ] Verification evidence reference and UTC time:

## Security

- [ ] Workflow dispatch is manual and protected-main-only
- [ ] Workflow repository permission is only `contents: read`
- [ ] Protected environment controls Jenkins variable and secret delivery
- [ ] Collaborator, fork, and no-other-workflow runner-targeting policy was checked
- [ ] Jenkins HTTP publication is loopback-only; no inbound-agent TCP port exists
- [ ] Administrator, handoff machine user, runner, and five agents are separate identities
- [ ] Handoff machine user has only reviewed read/build/cancel permission
- [ ] Runner is low privilege with no sudo, Docker socket, Jenkins state, or persistent Jenkins credential
- [ ] Each agent receives only its own registration secret and declared Stage secrets
- [ ] Security review evidence references and UTC time:

## Reconstruction

- [ ] Reconstruction began from the approved commit and absent controller volume
- [ ] Stack and runner preflights passed
- [ ] Controller has zero executors and the fixed job converged from tracked configuration
- [ ] Exactly five expected agent identities and labels have one executor each
- [ ] `exercise-live` proved clean isolated tmpfs and each installed role boundary twice
- [ ] Handoff machine-user preflight passed without exposing its token
- [ ] Reconstruction evidence references and UTC time:

## Restoration

- [ ] Recovery manifest schema, commit, exact pins, size, and SHA-256 were verified
- [ ] Restore target volume was absent or empty
- [ ] Fresh controller passwords and five secure agent-secret destinations were staged before restore
- [ ] The disposable recovery stack proved its replacement HMAC key and all five issued agent secrets before the target restore
- [ ] Complete controller volume restored and tracked configuration reconverged
- [ ] Controller, job, plugins, agents, labels, executors, and authorization passed `verify-live`
- [ ] Fresh credentials were validated before archived credentials were activated or replaced
- [ ] Restoration evidence references and UTC time:

## Live Run

- [ ] Dedicated runner was online with only `sdi-jenkins-handoff`
- [ ] One reviewed Fixture was dispatched from protected main
- [ ] GitHub reached the exact Jenkins build and published one accepted bundle and receipt
- [ ] Pipeline integration success was reported without a Domain or KPI claim
- [ ] Exact GitHub run URL:
- [ ] Exact GitHub artifact URL:

## Recovery

- [ ] New handoffs were quiesced and no Jenkins run was queued or building
- [ ] Jenkins stopped cleanly before snapshot
- [ ] Complete named controller volume was archived to operator-encrypted external storage
- [ ] Sanitized path-free manifest contains approved commit, pins, UTC timestamp, size, and SHA-256
- [ ] Archive and manifest permissions and external location policy were checked
- [ ] Recovery verification reference and UTC time:

## Walkthrough

- [ ] Operator demonstrated setup, preflight, start, status, logs, quiesce, and safe stop
- [ ] Operator demonstrated snapshot verification and explained empty-volume restore
- [ ] Maintainer demonstrated one-at-a-time runner, agent, and credential maintenance order
- [ ] Maintainer identified contract authorities, ownership seams, and repetition triggers
- [ ] Participants acknowledged that Fixture evidence is not Domain, Validation, deployment, or KPI evidence
- [ ] Walkthrough outcome and completion time (UTC):

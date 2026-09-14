# Pipeline Integration Workflow

`pipeline-integration.yml` is the sole external Pipeline integration trigger. It
accepts one committed run-request path on protected `main`, routes one handoff
job to the dedicated self-hosted runner through the protected
`pipeline-integration-jenkins` environment, invokes the repository-owned Jenkins
handoff command, publishes available evidence, and renders the validated final
summary.

See `docs/pipeline-integration-trigger.md` for protection, trigger, observation,
retrieval, failure, and cancellation procedures. Jenkins remains private and no
workflow caller can supply its endpoint, credentials, job, or safety limits.

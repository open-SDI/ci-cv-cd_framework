# Pipeline Integration Workflow

`pipeline-integration.yml` is the sole external Pipeline integration trigger. It
accepts one committed run-request path on protected `main`, routes one handoff
job to the dedicated self-hosted runner, a Jenkins stack runner service, through
the protected `pipeline-integration-jenkins` environment, invokes the
repository-owned Jenkins handoff command, publishes available evidence, and
renders the validated final summary.

The checkout is a depth-1 blobless partial clone with a non-cone sparse
checkout that omits the unused `CV/` Gazebo assets, nearly all of the
repository's bytes. The CLI reads committed blobs with lazy fetching disabled,
so it fails closed outside that set. The runner's workspace is a tmpfs, so the
checkout directory and the non-cone patterns `actions/checkout` appends to it
persist only until the runner container restarts; restarting it with
`stack stop` and `stack start`, or `stack quiesce` and `stack resume`, clears
them.

See `docs/pipeline-integration-trigger.md` for protection, trigger, observation,
retrieval, failure, and cancellation procedures. Jenkins remains private and no
workflow caller can supply its endpoint, credentials, job, or safety limits.

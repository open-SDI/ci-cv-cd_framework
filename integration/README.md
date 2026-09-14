# Pipeline Integration Package

This independent `uv` project owns the supported `sdi-integration` CLI and the
deep `sdi_pipeline_integration` package boundary. The CLI, committed file
contracts, and the language-neutral Stage-adapter process interface are the
supported interfaces. Internal Python modules are not compatibility contracts.

Run all checks through `scripts/verify` rather than using an ambient Python
environment.

## Identify A Committed Run

`identify-run` reads the request and every referenced input directly from one
resolved Git commit. It accepts only the protected-main dispatch ref and never
reads dirty or staged worktree content:

```sh
uv run --project integration sdi-integration identify-run \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-01-fixture.yaml
```

The repository must have one uncredentialed GitHub `origin` URL. Successful
validation emits one JSON record containing a newly assigned lowercase UUIDv4
Execution ID and exact-byte provenance. Validation failures exit with status 2,
write no record, and assign no Execution ID.

## Execute One Stage

`execute-stage` identifies the committed run, selects its reviewed adapter
descriptor, verifies the descriptor-bound Stage profile, and invokes the adapter
through the one-shot process interface. It accepts declared files only after the
complete candidate bundle, response correlation, file limits, and Domain schemas
validate:

```sh
uv run --project integration sdi-integration execute-stage \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-01-fixture.yaml \
  --descriptor-path deployment/jenkins/adapters/composition-fixture-v1.yaml \
  --attempt-root artifacts/composition-attempt
```

The adapter process exposes only:

```sh
sdi-fixture-adapter run \
  --request request.json \
  --input-root inputs \
  --output-root candidate
```

The accepted envelope records `execution_conclusion: succeeded`,
`implementation_mode: fixture`, and `domain_outcome: not_evaluated` separately.
The deterministic blueprint and deployment schema prove pipeline-interface
handling only. They are not composition, deployment, Validation, or KPI evidence.

## Dispatch The Four-Stage Fixture

`dispatch-local` assigns one Execution ID, executes the reviewed composition,
image-build, CV, and CD descriptors sequentially, and passes only exact accepted
files along the least-required graph. It atomically publishes a complete archive
candidate only after all attempts, Domain outputs, diagnostics, result metadata,
and checksums validate:

```sh
uv run --project integration sdi-integration dispatch-local \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-01-fixture.yaml \
  --bundle-root artifacts/s-04-tc-03-c-01
```

The archive contains the fixed `pipeline-integration-result.json`, five small
Domain handoff files, and four bounded diagnostics. The result is the manifest;
its checksummed inventory covers every other archive file without a recursive
self-digest. Revalidate the complete archive independently with:

```sh
uv run --project integration sdi-integration validate-bundle \
  --bundle-root artifacts/s-04-tc-03-c-01
```

Every attempt remains `implementation_mode: fixture` and
`domain_outcome: not_evaluated`; the result explicitly records
`kpi_evaluation: not_evaluated` and contains no overall Domain or six-combination
verdict.

## Jenkins Pipeline Operations

The root `Jenkinsfile` composes five public CLI operations rather than parsing or
constructing contracts in Groovy:

- `preflight-jenkins-run` preserves the submitted Execution ID and revalidates
  the seven scalar handoff values, committed inputs, all descriptors and Stage
  profiles, settled default limits, and the allocated resource mapping.
- `execute-jenkins-stage` reconstructs prior accepted attempts from bounded
  Jenkins stashes, validates every transferred byte and the trusted deployment
  work limit, and executes only the next reviewed descriptor on its matching
  immutable agent. An expired run deadline returns control without launching an
  adapter so integration-owned finalization can create the typed skip.
- `attempt-allows-continuation` derives whether another Domain Stage may run.
- `finalize-jenkins-run` creates typed downstream skips, assembles the exact
  archive, and validates it on the integration agent.
- `bundle-conclusion` revalidates the bundle and derives Jenkins machinery
  success without converting a valid negative Domain outcome into machinery
  failure.

These commands are pipeline-facing primitives. Operators submit only through the
fixed Jenkins job; they do not manually coordinate the primitives or reuse their
temporary directories as result authority.

## Handoff One Run To Jenkins

`handoff-jenkins` is the repository-owned transport seam used by the protected
GitHub workflow. It revalidates an already identified committed run,
performs an authenticated Jenkins preflight, submits the seven accepted scalar
parameters, follows the returned queue item to its exact executable build, and
retrieves only that build's declared archive:

```sh
uv run --project integration sdi-integration handoff-jenkins \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-01-fixture.yaml \
  --execution-id "$EXECUTION_ID" \
  --github-run-id "$GITHUB_RUN_ID" \
  --github-run-attempt "$GITHUB_RUN_ATTEMPT" \
  --bundle-root artifacts/bundle \
  --receipt-path artifacts/handoff-receipt.json
```

The Jenkins endpoint, fixed job, repository identity, machine username, API
token, and safety limits are trusted environment configuration rather than
command arguments or workflow inputs. The required settings are
`SDI_JENKINS_BASE_URL`, `SDI_JENKINS_JOB_PATH`, `SDI_JENKINS_REPOSITORY`,
`SDI_JENKINS_USERNAME`, and `SDI_JENKINS_API_TOKEN`. Plain HTTP is accepted only
on loopback; relocated Jenkins endpoints require verified HTTPS. The command
does not support passwords, crumbs, remote-trigger tokens, URL credentials, or
redirects. Preflight verifies the authenticated machine identity and access to
the configured fixed job before submission.

The independently configurable safety settings and repository defaults are:

| Setting | Default | Scope |
| --- | ---: | --- |
| `SDI_HANDOFF_HTTP_LIMIT_SECONDS` | 30 | One HTTP operation |
| `SDI_HANDOFF_SUBMISSION_LIMIT_SECONDS` | 30 | Build submission |
| `SDI_HANDOFF_QUEUE_WAIT_LIMIT_SECONDS` | 900 | Queue assignment |
| `SDI_HANDOFF_JENKINS_EXECUTION_LIMIT_SECONDS` | 5400 | Exact build execution |
| `SDI_HANDOFF_RETRIEVAL_LIMIT_SECONDS` | 300 | Complete artifact retrieval |
| `SDI_HANDOFF_RUNNER_OUTER_LIMIT_SECONDS` | 6000 | Complete handoff command |
| `SDI_HANDOFF_GITHUB_JOB_LIMIT_SECONDS` | 6600 | Enclosing GitHub job |

`SDI_HANDOFF_POLL_INTERVAL_SECONDS` is an optional trusted polling control and
defaults to two seconds. These values are safety limits, not performance claims.
The GitHub job limit must exceed the runner outer limit so an always-run caller
can publish available evidence.

The command emits only sanitized phase transitions. It never retries a failed
HTTP operation or reconciles an ambiguous submission, and it never consults a
latest-build pointer. `SIGINT` or `SIGTERM` causes one bounded cancellation POST
to the known queue item or exact build followed by a brief bounded observation
window when possible.

`handoff-receipt.json` is atomically updated outside the returned bundle as
queue and build facts become known. It contains relative Jenkins references,
never the private base URL or credentials. A `SUCCESS` build is accepted only
after complete schema, Execution-ID, path, size, digest, and undeclared-file
validation. A Jenkins `FAILURE` can still return a valid diagnostic bundle, but
the handoff exits unsuccessfully. `ABORTED`, missing, malformed, mismatched, or
incomplete evidence likewise fails with the available partial receipt.

The workflow captures the command's result record rather than printing it into
live logs, so the visible handoff output remains the native sanitized phase
sequence. Its always-run publication step uploads the validated bundle and
available receipt as the sole 90-day artifact named
`pipeline-integration-<execution_id>`.

After publication, `github-summary` independently validates the receipt and any
returned bundle, incorporates GitHub's handoff and publication outcomes, renders
the status-first summary, and returns the final machinery conclusion. It never
derives an aggregate Domain verdict. A result-less failed or cancelled handoff
is summarized from its receipt without promising Stage evidence.

Authenticated operators can submit the reviewed six S-04 request paths
sequentially through the same workflow with:

```sh
uv run --frozen --project integration sdi-integration dispatch-s-04 \
  --repository open-SDI/ci-cv-cd_framework
```

The helper prints each exact run URL before waiting and stops on failed or
cancelled integration. It does not introduce a batch record or KPI
interpretation. See `docs/pipeline-integration-trigger.md` for all UI, CLI, and
REST trigger and retrieval procedures.

## Settled Failure Semantics

`dispatch-local` publishes a complete contract-valid bundle whenever finalization
remains possible. Handled failures, crashes, deadline expiry, rejected candidate
transactions, unavailable implementations, and blocked dependencies appear as
typed attempts; skipped attempts never imply that a process ran. Adapter or
deadline failures return status 1 after printing the result. A valid negative
Domain outcome still returns status 0 because adapter execution succeeded.

Candidate acceptance is transactional. A malformed, missing, changing, unsafe,
undeclared, oversized, identity-mismatched, or schema-invalid candidate
contributes no adapter-authored files. The resulting failed attempt explicitly
records that no adapter response was accepted. An implemented adapter cannot
consume unevaluated Fixture output, while an implemented output may feed a later
Fixture during incremental replacement.

External `SIGINT` or `SIGTERM` cancellation is the deliberate exception: the CLI
immediately terminates the active adapter process tree, removes attempt-local and
bundle staging state, and publishes neither Stage attempts nor a Pipeline
integration result.

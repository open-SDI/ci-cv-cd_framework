# Pipeline Integration Package

This independent `uv` project owns the supported `sdi-integration` CLI and the
deep `sdi_pipeline_integration` package boundary. The CLI, committed file
contracts, and the language-neutral Stage-adapter process interface are the
supported interfaces. Internal Python modules are not compatibility contracts.

Run all checks through `scripts/verify` rather than using an ambient Python
environment.

Tests marked `live` call a real model provider; tests marked `slow` wait out a
real work limit or deadline. `scripts/verify` runs every test: the non-`slow`
tests in parallel, then the `slow` tests serially, because parallel load can
push their timing past its bounds. For a fast check while developing, run from
this directory:

```sh
uv run pytest -m "not live and not slow" -n auto --dist worksteal
```

## Identify A Committed Run

`identify-run` reads the request and every referenced input directly from one
resolved Git commit. It accepts only the protected-main dispatch ref and never
reads dirty or staged worktree content:

```sh
uv run --project integration sdi-integration identify-run \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-05-fixture.yaml
```

The repository must have one uncredentialed GitHub `origin` URL. Successful
validation emits one JSON record containing a newly assigned lowercase UUIDv4
Execution ID and exact-byte provenance. Validation failures exit with status 2,
write no record, and assign no Execution ID.

## Execute One Stage

`execute-stage` identifies the committed run, selects its reviewed adapter
descriptor, verifies that its Stage profile equals the one rendered from the
slot registry, and invokes the adapter
through the one-shot process interface. It accepts declared files only after the
complete candidate bundle, response correlation, file limits, and Domain schemas
validate:

```sh
uv run --project integration sdi-integration execute-stage \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-05-fixture.yaml \
  --descriptor-path deployment/jenkins/adapters/composition-v1.yaml \
  --service-repository service-repository \
  --attempt-root artifacts/composition-attempt
```

Only the composition Stage is granted the run-supplied composition inputs.
`execute-stage` and `execute-jenkins-stage` require `--service-repository` for
composition and reject `--service-repository`, `--composition-config`, and
`--generator` for any other Stage. `dispatch-local` runs composition, so it
always requires `--service-repository`. The Jenkins preflight takes only
`--composition-config` and `--generator`, to check the config's key names, and
`finalize-jenkins-run` takes none of them.
`--service-repository` names a directory of `SDI.md` descriptions; the runtime
copies every description into the composition Stage's inputs and gives the
adapter a manifest of their paths, sizes, and digests. It refuses symbolic
links, non-regular descriptions, and more than 256 files or 1 MiB.
`--composition-config` is a repository path read at the resolved commit and
defaults to `integration/composition-config.yaml`
(`sdi.composition-config/v1`), so a run can only pair a reviewed model endpoint
with the key variable that config names. The config names each generator with
its model, limits, context budget, endpoint and key variable, the
`default_generator`, and the `discovery` settings every generator shares. A run
uses the default generator (luna) unless `--generator NAME` names another the
config defines; an unknown name stops the run before launch. Nothing probes an
endpoint or falls back to another generator. The runtime resolves the config to
one declared, digest-checked input, `composition-settings.json`
(`sdi.composition-settings/v1`): the config's path and SHA-256, the selected
generator's name and settings, and the shared discovery settings. The committed
`qwen` generator is a self-hosted endpoint that keeps live tests cheap; it is not
a product choice. Discovery still embeds with OpenAI, so a qwen run also needs
`OPENAI_API_KEY`.

The adapter process exposes only:

```sh
sdi-composition-adapter run \
  --request request.json \
  --input-root inputs \
  --output-root candidate
```

## Implemented Composition

The composition descriptor selects `sdi-composition-adapter` with
`implementation_mode: implemented`. The adapter validates the copied
descriptions, discovers the ones the mission needs, asks the configured model
for proposals over those, assesses them against the whole repository with the
same rules as `assess-proposals`, and publishes the preferred proposal as the
composition blueprint and deployment schema. The preferred proposal supports the
most requirements, then has the fewest gaps; no performance evidence is
available, so equal proposals keep the model's order. The blueprint records the
`proposal_id` and each requirement's checked coverage status; each placement is
`resolved` or `unresolved`. A placement stays unresolved when its host omits the
device list or baseline the service's artifact needs. Both files and the
evidence carry the target profile's provenance basis as `profile_basis`, so a
placeholder profile stays labelled.

Discovery queries the service repository once per requirement, with its ID and
original Shall statement, and once with the profile's architectures, devices,
baselines and orchestrator interfaces. SQLite FTS5 BM25 ranks every
description's title, interfaces and body; when the composition config's
`discovery` has an `embeddings` block, cosine similarity over that endpoint's
embeddings ranks them too, and reciprocal rank fusion merges the two. The
committed config embeds with OpenAI's `text-embedding-3-small` for every
generator, so every generator ranks descriptions the same way. The orchestrator's inputs, then
each query's top three services in turn, are admitted along with every service
that provides one of their inputs or that their `depends_on` names. Admission
stops at the selected generator's `max_context_bytes`, a model-capacity limit,
and nothing fills the rest, so the model sees and may choose only the selected
descriptions. Without an `embeddings` block discovery is lexical only. With one,
an unset key or a failed embeddings call fails the run before generation, unless
`embeddings.on_unavailable: lexical` allows lexical-only discovery; the Stage's
reason summary and the `generate-proposals` stderr then say so, besides
`discovery.dense.status`.

The runtime forwards only the selected generator's key variable and the
embeddings key variable, and only when the descriptor's `secret_bindings` lists
them; otherwise `dispatch-local` and the Jenkins preflight reject the run before
any Stage starts. Each client reads only the key
paired with its own endpoint, so a generator's key never reaches the embeddings
endpoint and the embeddings key never reaches a generator. `HTTPS_PROXY`, `NO_PROXY`, and `SSL_CERT_FILE` are forwarded when
set. A candidate holding the forwarded key's exact bytes is rejected.

| Outcome code | Execution | Domain outcome | Published |
| --- | --- | --- | --- |
| `sdi.composition.preferred-proposal` | succeeded | succeeded | blueprint, deployment schema, evidence |
| `sdi.composition.bounded-no-result` | succeeded | failed | evidence |
| `sdi.composition.scoped-rejection` | succeeded | failed | evidence |
| `sdi.composition.insufficient-proposals` | succeeded | failed | evidence |
| `sdi.composition.discovery-no-result` | failed | not evaluated | diagnostic |
| `sdi.composition.discovery-over-budget` | failed | not evaluated | diagnostic |
| `sdi.composition.dense-discovery-unavailable` | failed | not evaluated | diagnostic |
| `sdi.composition.invalid-service-description` | failed | not evaluated | diagnostic |
| `sdi.composition.generation-invalid` | failed | not evaluated | diagnostic |
| `sdi.composition.generation-refused` | failed | not evaluated | diagnostic |
| `sdi.composition.provider-error` | failed | not evaluated | diagnostic |

Discovery fails with no result when no query retrieves any description, and
over budget when it retrieved some but none fits `max_context_bytes`; the
diagnostic counts the descriptions selected, omitted and left out for the
budget. A bounded result means generation reached `max_output_tokens`. A scoped
rejection means every proposal lost all of its services to placement removals.
`composition-evidence.json` (`sdi.composition-evidence/v1`, at most 256 KiB)
records the repository's description digests, the discovery trace, the
`composition_config_sha256`, the selected generator's name and resolved
settings, the exact model request and response summary, and the
complete assessment with its ranking order and, for each other eligible
proposal, the criterion that ranked it lower. The discovery trace holds each
query, its top ten fused, lexical and dense ranks, every selected description
with its reason and size, every omitted one with its reason (`not retrieved` or
`budget`), the input references left unexpanded, and the budget used. When no
proposal is preferred and discovery omitted descriptions, the assessment reason
says the result holds within the bounded supplied context. It is never an input
to a later Stage. Each assessed service carries its chosen artifact's `route` as
declared. A `mutable-artifact` finding marks a route that may install different
content later: any apt route, an image without a digest, or a source revision
that is not a full commit. It is read from the declared route only, never
resolved against a registry, and never counts as a gap. Reading the repository
fails rather than truncates at its limits, so the digests omit no description.
Reason summaries and the diagnostic come from fixed templates
and never repeat provider error text. Once the composition config is read, the
diagnostic names its digest and the selected generator, model, reasoning effort,
output limit, effective timeout and context budget, so a failed attempt still
records the settings it ran with. A failed attempt publishes no evidence, and
its diagnostic says so.

Fixture cases match exact input digests and cannot recognize real output, so a
Fixture Stage whose inputs include implemented output is skipped with
`sdi.dependency.implemented-evidence`. A successful composition therefore ends
the run with image build skipped and CV and CD blocked; the run still exits 0.

## Dispatch A Local Run

`dispatch-local` assigns one Execution ID, executes the reviewed composition,
image-build, CV, and CD descriptors sequentially, and passes only exact accepted
files along the least-required graph. It atomically publishes a complete archive
candidate only after all attempts, Domain outputs, evidence, diagnostics, result
metadata, and checksums validate:

```sh
uv run --project integration sdi-integration dispatch-local \
  --repository . \
  --requested-ref refs/heads/main \
  --resolved-commit "$GITHUB_SHA" \
  --run-request-path runs/s-04/s-04-tc-03-c-05-fixture.yaml \
  --service-repository service-repository \
  --bundle-root artifacts/s-04-tc-03-c-05
```

Export `OPENAI_API_KEY`, which luna and the embeddings use. To spend less while
the self-hosted endpoint serves, add `--generator qwen` and also export
`VLLM_KEY`. The result is the manifest; its checksummed inventory covers every
other archive file without a recursive self-digest. Revalidate the complete
archive independently with:

```sh
uv run --project integration sdi-integration validate-bundle \
  --bundle-root artifacts/s-04-tc-03-c-05
```

Image-build, CV, and CD remain Fixtures. The result explicitly records
`kpi_evaluation: not_evaluated` and contains no overall Domain or
six-combination verdict.

## Jenkins Pipeline Operations

The root `Jenkinsfile` composes five public CLI operations rather than parsing or
constructing contracts in Groovy:

- `preflight-jenkins-run` first compares the integration code digest each of
  the five agent images recorded at build time with the digest of the
  submitted checkout. It then preserves the submitted Execution ID and
  revalidates the seven scalar handoff values, committed inputs, all
  descriptors and Stage profiles, settled default limits, and the allocated
  resource mapping.
- `execute-jenkins-stage` first repeats that digest comparison for its own
  agent. It then reconstructs prior accepted attempts from bounded Jenkins
  stashes, validates every transferred byte and the trusted deployment work
  limit, and executes only the next reviewed descriptor on its matching
  immutable agent. An expired run deadline returns control without launching an
  adapter so integration-owned finalization can create the typed skip.
- `attempt-allows-continuation` derives whether another Domain Stage may run.
- `finalize-jenkins-run` creates typed downstream skips, assembles the exact
  archive, and validates it on the integration agent.
- `bundle-conclusion` revalidates the bundle and derives Jenkins machinery
  success without converting a valid negative Domain outcome into machinery
  failure.

A digest mismatch exits with status 4 and the reason
`sdi.agent.stale-image`, naming the stale agents, before any Stage work. When
the integration agent itself is stale, it names only that agent, because its
digest of the checkout cannot be trusted. `integration-code-digest --root
<integration directory>` prints the digest over `pyproject.toml`, `uv.lock`,
`src`, and `fixtures`, which the agent image build records in
`/etc/sdi/integration-code-digest`.

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
for loopback hosts and exactly the host `controller`, which the stack runner
reaches over its internal network; relocated Jenkins endpoints require verified
HTTPS. The command
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
consume unevaluated Fixture output, and a Fixture never consumes implemented
output. A negative Domain outcome publishes no Domain output.

External `SIGINT` or `SIGTERM` cancellation is the deliberate exception: the CLI
immediately terminates the active adapter process tree, removes attempt-local and
bundle staging state, and publishes neither Stage attempts nor a Pipeline
integration result.

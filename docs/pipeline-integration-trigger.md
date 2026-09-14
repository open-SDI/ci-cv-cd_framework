# Trigger And Retrieve Pipeline Integration

The `Pipeline integration` GitHub workflow is the complete external trigger,
progress, result, and retrieval surface. Jenkins remains private. Every trigger
uses the same one-field contract: protected `main` plus one committed
`run_request_path` string. Callers cannot select a commit, Jenkins endpoint,
credential, job, or timeout.

## Repository Protection

Before enabling dispatch:

1. Protect `main` against direct or unreviewed changes.
2. Create the GitHub environment `pipeline-integration-jenkins`.
3. Restrict that environment's deployment branches to protected branches only.
4. Add required reviewers when the repository's deployment policy requires
   human release approval.
5. Add environment variables `SDI_JENKINS_BASE_URL`,
   `SDI_JENKINS_JOB_PATH`, `SDI_JENKINS_REPOSITORY`, and
   `SDI_JENKINS_USERNAME`.
6. Add environment secret `SDI_JENKINS_API_TOKEN` containing the named Jenkins
   machine user's API token, not its password.
7. Require approval for every external fork workflow and never approve untrusted
   workflow code for this repository-scoped self-hosted runner.
8. Register only the dedicated runner described in
   `deployment/github-runner/README.md` with default labels disabled and the sole
   custom label `sdi-jenkins-handoff`.
9. Permit no other repository workflow to target `sdi-jenkins-handoff`.

For the tracked local Jenkins deployment, use the loopback base URL, job path
`pipeline-integration`, repository identity
`github.com/open-SDI/ci-cv-cd_framework`, and username `github-handoff`. The
runner must be on the Jenkins host because Jenkins is not externally published.

The workflow's job-level `github.ref == 'refs/heads/main'` condition prevents a
non-`main` dispatch from being routed to the privileged runner or requesting the
environment. The environment's protected-branch restriction is an independent
secret-release control. The job grants only `contents: read`.

The supported installation uses a repository-scoped runner even though the
upstream repository belongs to an organization. Its threat model trusts every
write collaborator and protected-main reviewer and treats the custom label as
an operational selector. Public users cannot manually dispatch the workflow.
Never approve untrusted external-fork workflow code for this runner. An
organization workflow-restricted runner group requires a separately reviewed
extension to the runner interface before it can replace this configuration.

## Dispatch One Request

### GitHub UI

Open **Actions > Pipeline integration > Run workflow**, select `main`, enter one
reviewed path in `run_request_path`, and choose **Run workflow**. Selecting any
other ref creates no handoff job.

### GitHub CLI

```sh acceptance=github-cli-dispatch
gh workflow run pipeline-integration.yml \
  --ref main \
  --field run_request_path=runs/s-04/s-04-tc-03-c-01-fixture.yaml
```

The current CLI prints the created run URL when GitHub returns it. Use its run
ID for every subsequent status, cancellation, and retrieval command; do not
search for the latest run.

### GitHub REST

```sh acceptance=github-rest-dispatch
curl --fail-with-body \
  --request POST \
  --header 'Accept: application/vnd.github+json' \
  --header 'Authorization: Bearer <fine-grained-token>' \
  --header 'X-GitHub-Api-Version: 2026-03-10' \
  https://api.github.com/repos/open-SDI/ci-cv-cd_framework/actions/workflows/pipeline-integration.yml/dispatches \
  --data '{"ref":"main","inputs":{"run_request_path":"runs/s-04/s-04-tc-03-c-01-fixture.yaml"}}'
```

The successful response contains `workflow_run_id`, API `run_url`, and exact
browser `html_url`. Persist that returned identity rather than inferring a run
from timestamps or listing order.

## Dispatch The Six S-04 Fixtures

The reviewed request paths, in sequential helper order, are:

1. `runs/s-04/s-04-tc-03-c-01-fixture.yaml`
2. `runs/s-04/s-04-tc-03-c-02-fixture.yaml`
3. `runs/s-04/s-04-tc-03-c-03-fixture.yaml`
4. `runs/s-04/s-04-tc-03-c-04-fixture.yaml`
5. `runs/s-04/s-04-tc-03-c-05-fixture.yaml`
6. `runs/s-04/s-04-tc-03-c-06-fixture.yaml`

Run all six through the same workflow contract:

```sh acceptance=s04-dispatch
uv run --frozen --project integration sdi-integration dispatch-s-04 \
  --repository open-SDI/ci-cv-cd_framework
```

The helper uses authenticated `gh`, prints each exact returned browser URL,
waits for that exact run to finish, and only then submits the next path. It stops
on the first failed or cancelled run. The helper creates no batch identity,
aggregate result, Domain verdict, or KPI claim.

## Observe And Retrieve

A queued run may be waiting for environment approval, for the dedicated runner
to become idle, or for an offline runner to reconnect. GitHub is authoritative
for this state; an absent Jenkins queue item is expected until the visible
handoff step reaches its `submitting` phase.

Observe the exact run:

```sh acceptance=github-observe
gh run watch <workflow-run-id> \
  --repo open-SDI/ci-cv-cd_framework \
  --exit-status

gh run view <workflow-run-id> \
  --repo open-SDI/ci-cv-cd_framework
```

After the summary reports the Execution ID, retrieve its one artifact:

```sh acceptance=github-download
gh run download <workflow-run-id> \
  --repo open-SDI/ci-cv-cd_framework \
  --name pipeline-integration-<execution-id> \
  --dir artifacts/pipeline-integration-<execution-id>
```

The artifact is retained for 90 days, subject to any shorter repository or
organization retention cap. A completed run with a returned result contains
`bundle/pipeline-integration-result.json`, every checksummed declared Domain
output and diagnostic beneath `bundle/stages/`, and
`handoff-receipt.json`. A failure before result retrieval can publish a
receipt-only artifact under the same Execution-ID-qualified name.

Revalidate a downloaded complete bundle independently:

```sh acceptance=downloaded-bundle-validation
uv run --frozen --project integration sdi-integration validate-bundle \
  --bundle-root artifacts/pipeline-integration-<execution-id>/bundle
```

## Failure And Cancellation

The summary is status-first. Pipeline integration succeeds only after the exact
returned bundle validates and GitHub reports successful artifact publication.
The Stage table keeps lifecycle, execution conclusion, implementation mode, and
Domain outcome separate and includes typed explanations and evidence paths.

A valid negative Domain outcome can remain green because the integration
machinery completed correctly. Jenkins machinery failure, retrieval failure,
contract rejection, identity mismatch, or artifact-publication failure is red.
A Jenkins `FAILURE` may still return and publish a valid diagnostic bundle; its
typed receipt remains authoritative for the red handoff status.

Cancel only the exact GitHub run:

```sh acceptance=github-cancel
gh run cancel <workflow-run-id> --repo open-SDI/ci-cv-cd_framework
```

GitHub retains the cancelled conclusion. The foreground handoff command asks
Jenkins to cancel only its correlated queue item or exact build and writes a
cancelled receipt when the bounded shutdown window permits. Cancellation does
not promise a Pipeline integration result. An always-run publication step makes
the available receipt retrievable when GitHub still permits post-cancellation
steps to complete.

Every current Stage uses a deterministic Fixture. Fixture files prove only the
pipeline interface and evidence handling. They are not proof of a real Domain
capability, CV verdict, deployment outcome, supported-combination KPI, or any
aggregate Domain result. The summary therefore always states
`KPI evaluation: not evaluated`.

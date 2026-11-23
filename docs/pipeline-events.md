# Pipeline Events

While a Jenkins build runs one combination through Composition, Image build,
CV and CD, the `sdi-integration` CLI and the Stage runtime print pipeline
events on stderr. This page is the reference for their lines and fields, and
for the checker that proves a console follows them.

A pipeline event is a pair of lines, written together in one `write`:

```text
[sdi] C-05 CV > stage-started  RUNNING  CV started
PRISM_EVENT {"schema_version":"sdi.pipeline-event/v1",...}
```

- The `PRISM_EVENT` line is the contract. It is a full snapshot of the
  combination's row.
- The `[sdi]` line is for people. Nothing parses it.
- Events are live and best-effort. The Pipeline integration result wins on any
  disagreement.
- The line with `"final":true` is the only authoritative line.

The row model is `PipelineEventRow` in
`integration/src/sdi_pipeline_integration/_pipeline_events.py`. The step
catalogue is in
`integration/src/sdi_pipeline_integration/_pipeline_event_catalogue.py`. The
generated schema is `integration/schemas/pipeline-event-v1.schema.json`.

## Line Grammar

### The `PRISM_EVENT` Line

- It starts at column 0 with `PRISM_EVENT `, followed by compact JSON on the
  same line: no spaces between tokens, and ASCII only.
- Keys come in the model's field order, the order of the tables below. Do not
  depend on that order.
- Ignore fields you do not know. Version 1 only adds fields; no field is
  renamed, and no field changes meaning.
- The generated schema allows unknown properties. The model itself forbids
  them, so the CLI's own checks are stricter than the schema.

### The `[sdi]` Line

- Stage level: `[sdi] {composition_id} {STAGE} > {step}  {status}  {message}`.
  `{STAGE}` is `COMPOSITION`, `IMAGE-BUILD`, `CV` or `CD`, and `{status}` is
  that Stage's own status field.
- Run level: `[sdi] {composition_id} RUN > {step}  {message}`, with no status
  column.
- The separator is the ASCII `>`. Two spaces separate the step, the status and
  the message.

### Characters and Size

Both lines are pure ASCII. Apart from the terminating LF, neither contains a
control character, DEL or the sequence `##[`.

| Character | `PRISM_EVENT` line | `[sdi]` line |
| -- | -- | -- |
| A line break in a slot value | removed from the message | removed from the message |
| Any other character outside 0x20 to 0x7E | `\uXXXX`, as JSON writes it | `\uXXXX`, the same escape |
| A character above U+FFFF | a `\uXXXX\uXXXX` surrogate pair | the same surrogate pair |
| A backslash | `\\`, as JSON writes it | `\\`, so every escape can be read back |
| The `[` of `##[` | `\u005b` | `\u005b` |

`\u005b` is a valid JSON escape, so a JSON parser reads `##[` back.

A `PRISM_EVENT` line has no size limit, so a row is always written whole.

An `[sdi]` line is at most 1024 bytes. Every template fits with
maximum-length slots. If a message is still too long, its identifier slots
(Combination ID, profile id, generator, service id, host and Reason code) are
shown as `-`, and an internal error line is printed.

A failed write to stderr is ignored. It never raises, and it never changes a
result or an exit code.

## Fields

Every field is present on every line, and is `null` until it is known. A
closed field is an enum the schema enforces. Every other code-like value is a
string, and the schema lists its known values as `examples`.

### Envelope

| Field | Value |
| -- | -- |
| `schema_version` | `"sdi.pipeline-event/v1"` |
| `emitted_at` | UTC, RFC 3339, milliseconds, `Z` suffix |
| `execution_id` | the run's Execution ID |
| `final` | `true` only on the `run-finished` line |
| `stage` | closed: `composition`, `image_build`, `cv`, `cd`, or null on run-level lines |
| `step` | a step code from the catalogue below |
| `message` | rendered from the step's template, never free text |

### Row Identity

| Field | Value |
| -- | -- |
| `composition_id` | the Combination ID, such as `"C-05"` |
| `scenario_id`, `testcase_id`, `profile_id` | from the run request |
| `target` | closed: `"SDV"`, `"SDR"`, or null |
| `architecture` | closed: `"amd64"`, `"arm64"`, or null |

### Statuses

A status is one of `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED` and
`NOT_EVALUATED`.

| Field | Value |
| -- | -- |
| `composition_status`, `image_build_status` | each CI Stage's own status |
| `ci_status` | derived from the two CI halves |
| `cv_status` | CV's status |
| `cd_status` | CD's status |
| `failure_reason` | a Reason code, or null |

`ci_status` is `PENDING` while both halves are `PENDING` and `RUNNING` while
either half runs. When both halves are terminal, it is the worse of the two,
ranked `FAILED`, `SKIPPED`, `NOT_EVALUATED`, `SUCCEEDED`. When one half is
terminal and the other is `PENDING`, it is `RUNNING` during a run, and the
terminal half's status in an `execute-stage` row.

A Stage's own status is `RUNNING` on each of its runtime lines before its
`stage-finished` line, and terminal on that line. It reads `SUCCEEDED` first
on its own `stage-finished` line, and it stays `SUCCEEDED` on every later line
except possibly the final one.

### CI Results

| Field | Value |
| -- | -- |
| `ci_duration_ms` | Composition's and Image build's process durations, summed |
| `blueprint_ref` | `stages/composition/composition-blueprint.json` |
| `deployment_schema_ref` | `stages/composition/deployment-schema.json` |
| `image_build_result_ref` | `stages/image-build/image-build-result.json` |
| `image_ref` | one tag per built image, sorted by service id |
| `image_digest` | each built image's digest, in the same order |
| `image_archive` | always null |
| `images_built`, `images_total` | built images, and every service in the deployment schema |
| `image_failures` | `{"service_id", "reason"}` for each failed image |
| `platform_status` | closed: `"MATCH"`, `"MISMATCH"`, or null |
| `detected_platform` | `linux/<architecture>` for the row's architecture, or null |

### CV Results

| Field | Value |
| -- | -- |
| `cv_duration_ms` | CV's process duration |
| `validation_evidence_ref` | `stages/cv/validation-evidence.json` |
| `cv_platform` | the platform the evidence records, such as `"linux/amd64"` |
| `cv_services_participating`, `cv_services_blocked`, `cv_services_total` | service counts |
| `cv_service_issues` | `{"service_id", "participation", "reason"}` for each service that did not participate or was not evaluated |
| `mission_setup` | `passed`, `timed_out`, or null |
| `mission_steps` | the eight S-04 steps in order, each `{"id", "status"}` |

### CD Results

| Field | Value |
| -- | -- |
| `cd_duration_ms` | CD's process duration |
| `deployment_result_ref` | `stages/cd/deployment-result.json` |
| `cd_placements_deployed`, `cd_placements_blocked`, `cd_placements_total` | placement counts |
| `placement_issues` | `{"service_id", "host", "status", "reason"}` for each placement that is neither deployed nor blocked |

A failed placement's reason is `CrashLoopBackOff`, `ErrImagePull`,
`ImagePullBackOff`, `Unschedulable` or `other`.

## Where Values Come From

`build_row` in
`integration/src/sdi_pipeline_integration/_pipeline_event_rows.py` builds
every row. It reads the run's identity, the committed target profile, and the
accepted Stage attempts its caller holds. The caller names only the line: its
Stage, its step, and the slots only the caller knows, such as `{limit_min}`.
The row fills every other slot, so a message cannot contradict its row.

`target` is the profile's robot kind, upper-cased. `architecture` is the one
all hosts share, or else the one that differs from the `native` host's. When
the profile cannot be read, or its architecture is ambiguous, both are null.

A Stage's status comes from its accepted attempt:

| Attempt | Status |
| -- | -- |
| none yet | `PENDING` |
| none yet, on the Stage's own lines before `stage-finished` | `RUNNING` |
| none yet, after an attempt that blocks later Stages | `SKIPPED` until the final line |
| succeeded, Domain succeeded | `SUCCEEDED` |
| succeeded, Domain failed | `FAILED` |
| failed or timed out | `FAILED` |
| skipped because the run deadline passed | `FAILED` |
| succeeded, Domain not evaluated | `NOT_EVALUATED` |
| any other skip | `SKIPPED` |

`failure_reason` is the Reason of the first group that has a Stage with one:

1. a `FAILED` Stage;
2. a Stage skipped with `sdi.stage.not-implemented` or
   `sdi.dependency.fixture-evidence`;
3. a `NOT_EVALUATED` Stage, or any other skip.

Within a group the earliest Stage wins. `sdi.dependency.prerequisite-blocked`
is never chosen.

Counts, durations, refs and the image fields come only from accepted result
files. A ref is set when its Stage accepted the file. The image tags need the
deployment schema, so `image_ref`, `platform_status` and `detected_platform`
are null in an `execute-stage image_build` row.

While a Stage runs, its accepted adapter reports fill item fields. The first
report for an item wins.

| Report | Fills |
| -- | -- |
| `service-unfit`, `pull-blocked` | a `cv_service_issues` entry, `not_evaluated` |
| `mission-setup-done` | `mission_setup`, and `mission_steps` with every step null |
| `mission-step` | that step in `mission_steps`; a scripted step reads `scripted` |
| `pod-not-running` | a `placement_issues` entry, `failed` |

From the Stage's `stage-finished` line on, its result files win. A Stage that
ended without a result file, such as a CV that timed out, shows null there,
even where its live lines showed values.

## Steps

The runtime prints the run-level and every-Stage steps. A Stage's adapter
reports the Stage-specific steps through its progress channel. `{Stage}` is
`Composition`, `Image build`, `CV` or `CD`.

| Scope | Steps |
| -- | -- |
| Run level | `run-accepted`, `run-finished` |
| Every Stage | `stage-started`, `adapter-running`, `work-limit-reached`, `run-deadline-reached`, `checking-output`, `publishing`, `stage-finished` |
| Composition | `discovery`, `generation`, `assessment` |
| Image build | `builder-check`, `building`, `image-built`, `image-failed` |
| CV | `screening`, `service-unfit`, `leftover-cleanup`, `sim-cached`, `sim-build`, `pull`, `pull-blocked`, `start`, `mission`, `mission-setup-done`, `mission-step`, `collecting-result`, `teardown` |
| CD | `nodes-wait`, `nodes-ready`, `applying`, `pod-running`, `pod-not-running`, `settling`, `judging`, `cleanup` |

While `generation`, `sim-build`, `building` or `mission` is a Stage's last
step, the runtime repeats it every three minutes with its heartbeat message
and the same step code.

One Stage's runtime lines follow one of these paths:

| Case | Steps |
| -- | -- |
| Normal, or rejected output | `stage-started`, `adapter-running`, `checking-output`, `publishing`, `stage-finished` |
| The adapter cannot start | `stage-started`, `publishing`, `stage-finished` |
| Nonzero exit, signal, or surviving descendants | `stage-started`, `adapter-running`, `publishing`, `stage-finished` |
| Work limit or run deadline | `stage-started`, `adapter-running`, `work-limit-reached` or `run-deadline-reached`, `publishing`, `stage-finished` |
| Skipped before the adapter starts | `stage-finished` |

A Stage that is killed stops after any step of a path, before
`stage-finished`. Only the last Stage that printed can stop early.

## Who Prints

Events go to stderr only. Each command's stdout stays what it was: one JSON
document, or nothing.

| Command | Lines it prints |
| -- | -- |
| `preflight-jenkins-run` | `run-accepted`, as its last action, after every check passed |
| `execute-jenkins-stage` | the lines of its Stage |
| `finalize-jenkins-run` | the final `run-finished` line, as its last action, after the bundle is in place |
| `dispatch-local` | `run-accepted` before the first Stage, the lines of each Stage it runs, and the final `run-finished` line after the bundle is in place |
| `execute-stage` | the lines of its Stage, with no run-level line |

The Stage runtime prints a Stage's lines, whichever command runs the Stage.
It prints `stage-started` just before it starts the adapter, after the
Stage's inputs, request and environment are ready. A Stage that fails before
that point prints nothing. A Stage skipped before its adapter starts prints
only `stage-finished`.

These print nothing:

- `execute-jenkins-stage` when the run deadline has already passed (exit 3)
  or its agent image is stale (exit 4).
- A command that fails before its first line, such as
  `preflight-jenkins-run` when any check fails.
- A Stage that `dispatch-local` or Finalize skips because an earlier Stage
  blocked it. The final line still shows it as `SKIPPED`.
- `attempt-allows-continuation`, `bundle-conclusion`, `deployment-manifest`
  and `integration-code-digest`.

A process stopped by SIGINT or SIGTERM prints nothing more, so its Stage has
no `stage-finished` line.

`execute-stage` rows have single-Stage scope, as [Statuses](#statuses)
describes for `ci_status`. The command has no run deadline, so the only time
limit line it can print is `work-limit-reached`.

`{limit_min}` in `adapter-running` and `work-limit-reached` is how long the
runtime waits for the adapter: the Stage's work limit, or the time left
before the run deadline when that is shorter. It is rounded down to whole
minutes, and it is at least 1. When the time left before the run deadline is
the shorter one, the runtime prints `run-deadline-reached` instead of
`work-limit-reached`.

To check the events of a local run, keep its stderr and check the file:

```sh
uv run --frozen sdi-integration dispatch-local ... 2> stderr.txt
uv run --frozen sdi-integration check-events stderr.txt --real-run
```

## Checking a Console

`check-events` checks the events in a console file, such as a test's captured
stderr or a Jenkins `consoleText`. Run it from `integration/`:

```sh
uv run --frozen sdi-integration check-events consoleText --real-run
uv run --frozen sdi-integration check-events stderr.txt --golden g9-race
```

- `--real-run` checks the line rules, the Stage paths and the final line.
- `--golden NAME` also compares the runtime lines with the golden file
  `NAME.txt` in `integration/tests/data/pipeline-events/`, and checks its extra
  field assertions. `--goldens DIRECTORY` reads goldens from another directory.

Exit code 0 means the console conforms. Exit code 1 means it does not, and each
violation is one stdout line in the form `<file>:<line>: [<rule>] <detail>`.
Exit code 2 means the file or the golden could not be read.

The checker reads only lines that start at column 0 with `[sdi] ` or
`PRISM_EVENT `. Shell trace lines and other output are skipped, but each
`PRISM_EVENT` line needs its `[sdi]` line directly before it. A console
decorator such as a timestamp prefix fails the check. Each pair must be
byte-equal to the renderer's output for the row the `PRISM_EVENT` line holds.

The final line's statuses equal those of the last `stage-finished` line, or
all `PENDING` when there is none. When the final `failure_reason` is
`sdi.run.deadline-exceeded`, one Stage may instead become `FAILED` and every
later Stage `SKIPPED`. That Stage is the first Stage still `PENDING`, or the
Stage of the last `stage-finished` line, whatever status that line shows.

### Golden Files

A golden file holds one projection per runtime line, in order:

```text
<stage or RUN> <step> [final] [<composition> <image build> <cv> <cd>] <failure_reason or ->
```

The status letters are `P` (`PENDING`), `R` (`RUNNING`), `S` (`SUCCEEDED`),
`F` (`FAILED`), `K` (`SKIPPED`) and `N` (`NOT_EVALUATED`). Lines that start
with `#` are comments. An indented line asserts a field of the line above it:

| Assertion | Meaning |
| -- | -- |
| `<field> == <json>` | equals a JSON value |
| `<field> != null` | is set |
| `<field> == @<field>` | equals another field of the same line |
| `<field> == @<field> - <n>` | equals another integer field minus `n` |
| `<field> contains <json object>` | a list with an element that has these keys and values |
| `<field>[*].<key> != null` | a list whose elements all set this key |

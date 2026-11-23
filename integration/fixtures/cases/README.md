# Fixture Cases

Immutable deterministic Fixture cases are committed here as pipeline-interface
evidence. A Fixture never claims Domain behavior, Validation evidence, or KPI
success.

Each case is selected by exact declared input byte sizes and SHA-256 digests. Its
payload bytes and diagnostic are independently checked before the adapter
publishes them. The C-05 cases emit schema-valid composition, image-build, and
CD contract data with Domain outcome `not_evaluated`. Their exact bytes prove
only the pipeline interface and handoff graph. CV has no Fixture: it skips the
Fixture chain's output with `sdi.dependency.fixture-evidence`, which turns that
chain's run red. The committed
composition descriptor is implemented, so tests reach these cases by selecting a
Fixture composition descriptor. Composition cases match the service-repository
manifest and the composition settings resolved from the composition config
under `integration/tests/data/` with its default generator.

The dedicated requests under `runs/conformance/` select immutable cases for a
negative Domain outcome, handled execution failure, process crash, timeout,
absent or malformed response, missing or undeclared output, unsafe filesystem
output, oversized output, schema mismatch, and response identity mismatch. These
deliberately adverse cases prove runtime and contract handling only. Their
payloads and outcomes are not evidence that a Domain capability or KPI was
evaluated.

The input digests, payload `source_inputs`, and payload and diagnostic sizes and
digests are generated, so do not edit them by hand. After changing a run input,
the Fixture composition config or service repository under
`integration/tests/data/`, a payload, or a diagnostic, run
`integration/scripts/regenerate-fixtures --write`. It recomputes every case in
Stage order from the working tree, so an input change reaches every downstream
Stage. `integration/scripts/verify` runs `--check`, which names each stale file.
Every other value in a case or payload stays hand-authored, but the command owns
their layout: it writes each case in one fixed YAML form and each payload in one
fixed JSON encoding. Digests are over the exact file bytes, so a checkout that
rewrites line endings, such as `core.autocrlf` on Windows, does not match.

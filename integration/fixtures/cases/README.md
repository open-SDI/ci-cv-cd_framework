# Fixture Cases

Immutable deterministic Fixture cases are committed here as pipeline-interface
evidence. A Fixture never claims Domain behavior, Validation evidence, or KPI
success.

Each case is selected by exact declared input byte sizes and SHA-256 digests. Its
payload bytes and diagnostic are independently checked before the adapter
publishes them. The C-05 cases emit schema-valid composition, image-build, CV,
and CD contract data with Domain outcome `not_evaluated`. Their exact bytes prove
only the four-Stage pipeline interface and handoff graph. The committed
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

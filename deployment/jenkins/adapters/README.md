# Adapter Descriptors

Reviewed descriptors select a Stage, Implementation mode, digest-pinned image,
process-contract version, Stage-profile version, entry point, and logical secret
bindings. The agent label comes from the Stage registry, not the descriptor.
Secret values and Jenkins credential identifiers are governed separately and
never enter these contracts.

`composition-v1.yaml` selects the implemented `sdi-composition-adapter`. Its
`secret_bindings` name the environment variables a composition config may use
for its keys; the runtime forwards only the selected generator's key and the
embeddings key. `image-build-v1.yaml` selects the implemented
`sdi-image-build-adapter`, which drives the stack's BuildKit builder and pushes
to the pipeline registry; it receives no secrets. The CV and CD Fixture
descriptors, and `image-build-fixture-v1.yaml`, which `dispatch-local` uses
because the builder exists only inside the stack, receive no secrets and select
one shared digest-pinned Fixture implementation through separate Stage
profiles, each rendered from the slot registry and checked when the descriptor
loads.
Each Domain-agent build consumes its own descriptor-selected runtime image, so a
later reviewed descriptor can replace and reconcile one agent independently.
The `ci` node identity exposes the Stage registry's `composition` label; the
other three node identities and registry labels match.

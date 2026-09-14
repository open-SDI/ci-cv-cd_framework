# Adapter Descriptors

Reviewed descriptors select a Stage, Implementation mode, digest-pinned image,
process-contract version, Stage-profile version, agent label, entry point, and
logical secret bindings. Secret values and Jenkins credential identifiers are
governed separately and never enter these contracts.

The four Fixture descriptors receive no secrets and select one shared
digest-pinned Fixture implementation through separate reviewed Stage profiles.
Each Domain-agent build consumes its own descriptor-selected runtime image, so a
later reviewed descriptor can replace and reconcile one agent independently.
The `ci` node identity exposes the descriptor's `composition` label; the other
three node identities and logical labels match.

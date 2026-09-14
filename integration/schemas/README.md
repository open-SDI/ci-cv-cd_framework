# Versioned Schemas

Generated JSON Schemas for authoritative input, process, Domain output,
Pipeline integration result, and sanitized recovery-manifest contracts are
committed here. They are reviewable public file contracts derived from the
closed Pydantic models.
They expose closed structural shapes and lexical identifier and path rules. The
CLI additionally enforces Git-object, exact-byte, and cross-document semantics
that JSON Schema cannot express.

Regenerate them from `integration/` with:

```sh
uv run --frozen sdi-integration schemas --write --directory schemas
```

`scripts/verify` fails when a schema is missing, obsolete, or stale.

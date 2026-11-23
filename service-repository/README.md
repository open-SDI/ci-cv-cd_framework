# SDI Service Repository

Mobility service descriptions live here, one per folder as
`<service_id>/SDI.md`. Each file's YAML front matter holds the facts CI checks
and carries; its Markdown body describes purpose, capabilities, assumptions,
and limitations for the model. An omitted fact is unknown.

CI reads every `SDI.md` under this directory as a supplied read-only path and
never writes it. Stage commands take it as `--service-repository`; the runtime
copies the descriptions into the composition Stage's inputs with a manifest of
their digests. `basis: placeholder` descriptions stand in for services whose
facts are not yet declared upstream; their labels are carried, not checked.

Validate every description, including unique `service_id`s and acyclic
`depends_on` references, from the repository root with:

```sh
uv run --project integration sdi-integration validate-service-repository \
  --service-repository service-repository
```

The front matter follows
`integration/schemas/service-description-v1.schema.json`.

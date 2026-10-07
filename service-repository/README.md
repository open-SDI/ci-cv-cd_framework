# SDI Service Repository

Mobility service descriptions live here, one per folder as
`<service_id>/SDI.md`. Each file's YAML front matter holds the facts CI checks
and carries; its Markdown body describes purpose, capabilities, assumptions,
and limitations for the model. An omitted fact is unknown.

CI reads every `SDI.md` under this directory as a supplied read-only path and
never writes it. `basis: placeholder` descriptions stand in for services whose
facts are not yet declared upstream; their labels are carried, not checked.

# SDI Service Repository

Each mobility service has one service folder here, `<service_id>/`. The folder
holds the service's description, `<service_id>/SDI.md`, and may hold the
recipes that build its artifacts and the files they copy. The description's
YAML front matter holds the facts CI checks and carries; its Markdown body
describes purpose, capabilities, assumptions, and limitations for the model. An
omitted fact is unknown.

An artifact's optional `dockerfile:` names its recipe, a Dockerfile relative to
the service folder, which is also the build context. Two artifacts may share a
recipe. An artifact's `route` is provenance only: it records where the artifact
comes from and does not describe what a recipe builds. The recipe's `CMD` is the
service's default process.

CI reads this directory as a supplied read-only path and never writes it.
Composition and Image-build Stage commands take it as `--service-repository`;
the runtime copies every regular file in the service folders into the Stage's
inputs with a manifest of their paths, sizes, and digests. Files at this
directory's root, such as this README, are not copied. Composition reads only
each folder's `SDI.md`; a nested `SDI.md` is a plain file. `basis: placeholder`
descriptions stand in for services whose facts are not yet declared upstream;
their labels are carried, not checked.

The copy keeps file bytes, not file modes, so a recipe sets modes itself, for
example with `COPY --chmod=0755`. Every file and folder name starts with a
letter or digit and holds only letters, digits, `.`, `_`, and `-`, so dotfiles
such as `.dockerignore` are refused. Symbolic links, non-regular files, and more
than 256 files or 1 MiB in total are refused too.

Validate every description, including that each `service_id` equals its folder
name and that `depends_on` references are known and acyclic, from the
repository root with:

```sh
uv run --project integration sdi-integration validate-service-repository \
  --service-repository service-repository
```

The front matter follows
`integration/schemas/service-description-v1.schema.json`.

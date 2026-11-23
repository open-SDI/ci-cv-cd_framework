# Stage Profiles

Stage profiles define the immutable input, bounded output, diagnostic, and work
grants used by the permanent Stage-adapter interface. Each profile is rendered
from the slot registry and per-Stage grants in
`src/sdi_pipeline_integration/_stage_contracts.py`; never edit one by hand.
Change the registry, then run `sdi-integration stage-profiles --write` from
`integration/`. `scripts/verify` runs `--check`, and loading an adapter rejects
a profile that differs from its rendering by even one byte. A Stage's Domain
outputs are the slots it originates, so a new output slot needs no other edit;
a new input also joins the receiving Stage's grant.

Stage profiles enforce the least-required graph: composition outputs feed image
build; composition and image-build outputs feed CV; and all prior Domain outputs
feed CD. Initial requirements and target-profile inputs are granted only where
the reviewed graph requires them.

Agent images render profiles with the integration code they bake, so a changed
grant or limit takes effect in Jenkins only after the agent images are rebuilt.

Composition also receives the run-supplied service-repository manifest, the
composition settings resolved from the composition config, and may publish optional
`composition-evidence.json`. Evidence records how the Stage reached its outcome
and never feeds a later Stage. A negative Domain outcome publishes no Domain
output.

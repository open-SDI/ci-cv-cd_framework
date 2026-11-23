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
build; composition and image-build outputs feed CV and CD. CV and CD publish no
Domain output. Initial requirements and target-profile inputs are granted only
where the reviewed graph requires them.

Agent images render profiles with the integration code they bake, so a changed
grant or limit takes effect in Jenkins only after the agent images are rebuilt.

Image build and CV also receive the run-supplied service-repository manifest,
so they can read each service's description and recipes. Composition receives
that manifest, the composition settings resolved from the composition config,
and may publish optional `composition-evidence.json`. An implemented CV attempt
whose execution succeeded must publish `validation-evidence.json` with its
verdict, on every outcome. A CV error publishes none. An implemented CD
attempt whose execution succeeded must publish `deployment-result.json` with
its outcome in the same way, and the CD Fixture publishes none. Evidence records how the
Stage reached its outcome and never feeds a later Stage. A negative Domain
outcome publishes no Domain output.

A `stage_attachment` output is a file for people, which no check parses and no
later Stage reads. It is always optional, and only an attempt whose execution
succeeded may publish one. Beside its evidence, CV attaches
`sim-recording.mp4`, a 640x480, 10 fps H.264 video of the S-04 run from a fixed
overview camera. The bundle keeps it at `stages/cv/sim-recording.mp4`. When
the recording fails, CV publishes its verdict without it, and `diagnostic.txt`
names the cause on its `recording:` line.

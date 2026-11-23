# Stage Profiles

Reviewed Stage profiles define the immutable input, bounded output, diagnostic,
and work grants used by the permanent Stage-adapter interface. They enforce the
least-required graph: composition outputs feed image build; composition and
image-build outputs feed CV; and all prior Domain outputs feed CD. Initial
requirements and target-profile inputs are granted only where the reviewed graph
requires them.

Composition also receives the run-supplied service-repository manifest, the
composition settings resolved from the composition config, and may publish optional
`composition-evidence.json`. Evidence records how the Stage reached its outcome
and never feeds a later Stage. A negative Domain outcome publishes no Domain
output.

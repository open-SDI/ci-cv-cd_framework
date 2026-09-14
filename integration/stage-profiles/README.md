# Stage Profiles

Reviewed Stage profiles define the immutable input, bounded output, diagnostic,
and work grants used by the permanent Stage-adapter interface. They enforce the
least-required graph: composition outputs feed image build; composition and
image-build outputs feed CV; and all prior Domain outputs feed CD. Initial
requirements and target-profile inputs are granted only where the reviewed graph
requires them.

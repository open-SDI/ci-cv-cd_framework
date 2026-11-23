---
schema_version: sdi.service-description/v1
service_id: fake-digest-image
title: Service delivered as a container image by digest

provenance:
  basis: placeholder
  note: >-
    Test-only hypothetical description without a defect: its image is named by digest.

depends_on: []

artifacts:
  image:
    architectures: [arm64]
    route:
      kind: image
      reference: example.invalid/sdi-test-fakes:jazzy@sha256:4246189700b0fc2ce2d023ea4a5304d60af68a615d284020a1e221785b18b6e7
---

## Purpose

Test-only hypothetical service. It is not evidence that any real service exists.

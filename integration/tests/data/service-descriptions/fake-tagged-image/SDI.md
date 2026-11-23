---
schema_version: sdi.service-description/v1
service_id: fake-tagged-image
title: Service delivered as a container image by tag

provenance:
  basis: placeholder
  note: >-
    Test-only hypothetical description with one deliberate defect: its image is named by a movable tag, not a digest.

depends_on: []

artifacts:
  image:
    architectures: [arm64]
    route:
      kind: image
      reference: example.invalid/sdi-test-fakes:jazzy
---

## Purpose

Test-only hypothetical service. It is not evidence that any real service exists.

---
schema_version: sdi.service-description/v1
service_id: fake-unverified-endpoints
title: Service without role maps

provenance:
  basis: placeholder
  note: >-
    Test-only hypothetical description with one deliberate defect: it declares none of the six role maps.

devices: []
depends_on: []

artifacts:
  jazzy-source:
    architectures: [amd64, arm64]
    host_requirement:
      os: ubuntu-24.04
      ros_distro: jazzy
    route:
      kind: source
      repository: https://example.invalid/sdi-test-fakes
      revision: test-only
---

## Purpose

Test-only hypothetical service. It is not evidence that any real service exists.

---
schema_version: sdi.service-description/v1
service_id: fake-unmet-dependency
title: Service depending on nav2-localization

provenance:
  basis: placeholder
  note: >-
    Test-only hypothetical description with one deliberate defect: it depends on nav2-localization, which a test proposal leaves out.

devices: []
depends_on: [nav2-localization]

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

publishes:
  /fake/status: {type: std_msgs/msg/String, reliability: reliable, durability: volatile}
---

## Purpose

Test-only hypothetical service. It is not evidence that any real service exists.

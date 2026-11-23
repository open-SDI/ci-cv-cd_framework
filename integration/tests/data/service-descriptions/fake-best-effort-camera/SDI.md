---
schema_version: sdi.service-description/v1
service_id: fake-best-effort-camera
title: Camera publishing /image with best-effort QoS

provenance:
  basis: placeholder
  note: >-
    Test-only hypothetical description with one deliberate defect: /image is best_effort while face-recog requests reliable.

devices: [camera]
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

publishes:
  /image: {type: sensor_msgs/msg/Image, reliability: best_effort, durability: volatile}
---

## Purpose

Test-only hypothetical service. It is not evidence that any real service exists.

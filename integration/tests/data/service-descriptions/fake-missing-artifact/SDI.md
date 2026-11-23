---
schema_version: sdi.service-description/v1
service_id: fake-missing-artifact
title: Service without artifacts

provenance:
  basis: placeholder
  note: >-
    Test-only hypothetical description with one deliberate defect: it declares no artifacts.

devices: []
depends_on: []

publishes:
  /fake/heartbeat: {type: std_msgs/msg/Empty, reliability: reliable, durability: volatile}
---

## Purpose

Test-only hypothetical service. It is not evidence that any real service exists.

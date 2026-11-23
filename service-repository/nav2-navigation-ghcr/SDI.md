---
schema_version: sdi.service-description/v1
service_id: nav2-navigation-ghcr
title: Nav2 navigation stack from the upstream Jazzy CI container image

provenance:
  basis: declared
  source: >-
    From the ghcr.io/ros-navigation/navigation2:jazzy image manifest and config
    (labels: version 1.3.13, revision f4108e5b1c2bce804a1aa0c7be6673a8eb4a1501) and
    Nav2's .github/workflows/update_ci_image.yaml.

implementation:
  name: navigation2
  version: 1.3.13
  repository: https://github.com/ros-navigation/navigation2
  revision: f4108e5b1c2bce804a1aa0c7be6673a8eb4a1501

devices: []
depends_on: []

artifacts:
  ghcr-jazzy:
    architectures: [amd64]
    route:
      kind: image
      reference: ghcr.io/ros-navigation/navigation2:jazzy@sha256:42461897f0b0fc2ce2d023ea4a5304d60af68a615d284020a1e221785b18b6e7
    invocation: >-
      docker run --rm --network host
      ghcr.io/ros-navigation/navigation2:jazzy@sha256:42461897f0b0fc2ce2d023ea4a5304d60af68a615d284020a1e221785b18b6e7
      ros2 launch nav2_bringup navigation_launch.py use_sim_time:=false
---

## Purpose

Plans and follows a path to a goal pose on a known 2D map, like `nav2-navigation`,
but delivered as the upstream Nav2 Jazzy container image instead of Debian packages.

## Capabilities

- Offers the Nav2 navigation servers with Nav2's default parameters.

## Assumptions and preconditions

- Runs on an amd64 host with a container runtime and host networking.

## Limitations and known failure modes

- The image is built for amd64 only; it does not run on arm64 hosts.
- It is Nav2's CI builder image (with the source workspace), not a slim runtime image.
- It carries no TurtleBot3 parameters: with Nav2's defaults it publishes `Twist`
  rather than `TwistStamped` on `/cmd_vel` and assumes a different robot footprint.
- Endpoints are not listed.
- The `jazzy` tag is rebuilt on a schedule; the digest above is the one inspected.

## Configuration notes

Nav2 default parameters from `nav2_bringup/params/nav2_params.yaml` inside the image.

## Evaluation

None recorded.

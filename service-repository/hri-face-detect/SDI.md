---
schema_version: sdi.service-description/v1
service_id: hri-face-detect
title: ROS4HRI face detection (Humble only)

provenance:
  basis: declared
  source: >-
    From ros4hri hri_face_detect 2.2.2, branch humble-devel
    (599fde067a0e9360d107ad1bb1e63f07651b1e2b), hri_face_detect/node_face_detect.py,
    launch/face_detect.launch.py and config/00-defaults.yml, and the ros/rosdistro
    humble entry (source only, no binary release).

implementation:
  name: hri_face_detect
  version: 2.2.2
  repository: https://github.com/ros4hri/hri_face_detect
  revision: 599fde067a0e9360d107ad1bb1e63f07651b1e2b

devices: []
depends_on: []

artifacts:
  humble-source:
    architectures: [amd64, arm64]
    host_requirement:
      os: ubuntu-22.04
      ros_distro: humble
    route:
      kind: source
      repository: https://github.com/ros4hri/hri_face_detect
      revision: 599fde067a0e9360d107ad1bb1e63f07651b1e2b
    invocation: >-
      ros2 launch hri_face_detect face_detect.launch.py, with the remapping
      image: /image in its PAL configuration

subscribes:
  /image: {type: sensor_msgs/msg/Image, reliability: best_effort, durability: volatile}

publishes:
  /humans/faces/tracked: {type: hri_msgs/msg/IdsList, reliability: reliable, durability: volatile}
---

## Purpose

Detects faces in a camera stream and publishes them following the ROS4HRI
conventions, with a stable identifier per tracked face.

## Capabilities

- Publishes the list of currently tracked face IDs on `/humans/faces/tracked`.
- Per face, publishes its region of interest, landmarks and cropped and aligned
  face images under `/humans/faces/<id>/`, and broadcasts `face_<id>` and
  `gaze_<id>` transforms.

## Assumptions and preconditions

- Built from source for ROS 2 Humble on Ubuntu 22.04; rosdistro lists it for
  Humble only and has no binary release for any distribution.
- Its image input is the relative `image` topic; this service's fixed
  configuration remaps it to `/image` through the node's PAL configuration.
- Python dependencies are pinned by its `requirements.txt` (mediapipe 0.10.9,
  opencv-contrib-python 4.10.0.84, protobuf 3.20.3).

## Limitations and known failure modes

- Does not run on Ubuntu 24.04 / Jazzy hosts as declared.
- The subscriber is best effort; a reliable camera publisher is compatible, the
  reverse is not.
- Lists only `/image` and `/humans/faces/tracked`; it also subscribes to
  `camera_info` and publishes per-face topics and diagnostics.
- Runs on CPU; no GPU use was found.

## Configuration notes

Defaults kept: processing rate 30 Hz, confidence threshold 0.75, image scale 0.5,
face mesh on, random face IDs. The launch file configures and activates the
lifecycle node.

## Evaluation

None recorded.

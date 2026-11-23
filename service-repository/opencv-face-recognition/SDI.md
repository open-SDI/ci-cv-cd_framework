---
schema_version: sdi.service-description/v1
service_id: opencv-face-recognition
title: Face recognition of enrolled people with OpenCV on CPU

provenance:
  basis: declared
  source: >-
    From this folder's opencv_face_recognition.py, written for SDI, over
    OpenCV 4.6.0 (the noble python3-opencv package) and the YuNet
    face_detection_yunet_2022mar and SFace face_recognition_sface_2021dec
    models from opencv_zoo 7e062e54cf5410c09b795ff71b4a255e58498c79.

implementation:
  name: opencv_zoo
  repository: https://github.com/opencv/opencv_zoo
  revision: 7e062e54cf5410c09b795ff71b4a255e58498c79

devices: []
depends_on: []

artifacts:
  jazzy-debs:
    architectures: [amd64, arm64]
    host_requirement:
      os: ubuntu-24.04
      ros_distro: jazzy
    dockerfile: Dockerfile.jazzy-debs
    route:
      kind: apt
      packages:
        - python3-opencv=4.6.0+dfsg-13.1ubuntu1
        - ros-jazzy-rclpy=7.1.12-1*
        - ros-jazzy-sensor-msgs=5.3.8-1*
        - ros-jazzy-std-msgs=5.3.8-1*

subscribes:
  /image: {type: sensor_msgs/msg/Image, reliability: reliable, durability: volatile}
  /face_enrollment: {type: sensor_msgs/msg/Image, reliability: reliable, durability: transient_local}

publishes:
  /face_recognition/name: {type: std_msgs/msg/String, reliability: reliable, durability: volatile}
---

## Purpose

Verifies by face recognition who is in front of the robot's camera, on the
robot's CPU, so that the robot can confirm that the person present is an
enrolled recipient before it hands something over.

## Capabilities

- Enrolls a person from a reference photo published on `/face_enrollment`,
  with the person's name in `header.frame_id`. The largest face in the photo
  becomes that name's reference face; a later photo with the same name
  replaces it.
- Detects every face in each `/image` frame with YuNet and compares it with
  each reference face by SFace cosine similarity, at SFace's published
  threshold of 0.363.
- Publishes the name of each enrolled person it matches on
  `/face_recognition/name`, once per matched frame.

## Assumptions and preconditions

- Recognizes only enrolled people: it starts with no reference faces and
  publishes nothing until someone enrolls a photo with a name and one
  detectable face.
- A camera service provides `/image` reliably, as `rgb8`, `bgr8`, `rgba8`,
  `bgra8` or `mono8`. Other encodings are ignored with one warning.
- The face is frontal, well lit, and large enough in the frame for YuNet
  to detect it.

## Limitations and known failure modes

- Publishes nothing for an unknown or unenrolled face, and nothing when no
  face is seen, so silence does not distinguish "no face" from "a stranger".
- No liveness check: a printed photo of an enrolled person matches.
- No tracking or smoothing: each frame is judged alone, and a match can come
  and go between frames. It reports no confidence score.
- Enrollment is held in memory only and is lost on restart. The transient
  local enrollment topic keeps one photo, so a node that starts late sees
  only the most recent enrollment.
- Reference faces are personal data; enroll only people who consented.

## Configuration notes

The YuNet model is vendored in this folder with its MIT license
(`LICENSE.face_detection_yunet`). The recipe downloads the SFace model
(Apache 2.0) from the pinned opencv_zoo commit at build time and checks its
sha256. There are no parameters. The image lists its installed packages in
`/usr/share/sdi/installed-packages.txt`.

## Evaluation

None recorded.

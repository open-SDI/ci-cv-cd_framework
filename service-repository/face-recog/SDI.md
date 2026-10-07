---
schema_version: sdi.service-description/v1
service_id: face-recog
title: Face recognition of known people from /image

provenance:
  basis: placeholder
  note: >-
    Facts read from elpidiovaldez/face_recog 49604f569e1ef0c6283c2a8369f580167087b874
    (the version OrchestML 0a951cb lists), but the service as described needs a
    patched fork and a trained classifier that do not exist yet.

implementation:
  name: face_recog
  repository: https://github.com/elpidiovaldez/face_recog
  revision: 49604f569e1ef0c6283c2a8369f580167087b874

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
      repository: https://github.com/elpidiovaldez/face_recog
      revision: 49604f569e1ef0c6283c2a8369f580167087b874
    invocation: ros2 run face_recog live_face_recog

subscribes:
  /image: {type: sensor_msgs/msg/Image, reliability: reliable, durability: volatile}

publishes:
  /faces/detections: {type: vision_interfaces/msg/Detections, reliability: reliable, durability: volatile}
---

## Purpose

Recognises known people in camera images and reports where their faces are.

## Capabilities

- Detects faces in `/image` (MTCNN) and identifies them against a trained
  classifier over FaceNet embeddings.
- Publishes `/faces/detections` with each recognised face's bounding box, name and
  probability; only faces identified with probability above 0.85 are reported.

## Assumptions and preconditions

- A classifier trained on about 20 photos per known person, made with the
  repository's `review_training_data.py` and `make_classifier.py`.
- PyTorch and facenet-pytorch must be installed by hand; the package declares
  neither, and facenet-pytorch pins `torch<2.3`. Pretrained FaceNet weights
  download at first start.
- Uses `cuda:0` when available, otherwise the CPU.

## Limitations and known failure modes

- The route builds upstream as is, which hard-codes the classifier path to
  `/home/paul/.../face_classifier.pt` and so does not start here. The intended
  fork, not yet created, makes the path a parameter, loads it with
  `torch.load(..., weights_only=False)`, and depends on `python3-opencv` instead of `cv2`.
- `header.stamp` is the publish time; the input image header is in `image_header`.

## Configuration notes

`show_faces` (default false) opens a preview window; keep it off on headless hosts.

## Evaluation

None recorded.

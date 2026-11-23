---
schema_version: sdi.service-description/v1
service_id: v4l2-camera
title: USB camera images from a V4L2 device on /image

provenance:
  basis: declared
  source: >-
    From ros2_v4l2_camera 0.7.3 (098b1d5195b5ea972ce2e915ebb7eb8d30793e87)
    src/v4l2_camera.cpp and src/parameters.cpp, and image_transport's camera publisher.

implementation:
  name: v4l2_camera
  version: 0.7.3
  repository: https://gitlab.com/boldhearts/ros2_v4l2_camera
  revision: 098b1d5195b5ea972ce2e915ebb7eb8d30793e87

devices: [camera]
depends_on: []

artifacts:
  jazzy-debs:
    architectures: [amd64, arm64]
    host_requirement:
      os: ubuntu-24.04
      ros_distro: jazzy
    route:
      kind: apt
      packages:
        - ros-jazzy-v4l2-camera=0.7.3-1*
    invocation: >-
      ros2 run v4l2_camera v4l2_camera_node --ros-args
      -p video_device:=/dev/video0 -r image_raw:=image

publishes:
  /image: {type: sensor_msgs/msg/Image, reliability: reliable, durability: volatile}
---

## Purpose

Publishes raw images from a USB webcam (any V4L2 device) as a ROS image stream.

## Capabilities

- Publishes `sensor_msgs/Image` frames on `/image`, 640×480 `rgb8` by default.
- Publishes the matching `/camera_info`; it carries only the image size unless a
  calibration file is given with `camera_info_url`.

## Assumptions and preconditions

- A V4L2 camera is attached, by default `/dev/video0`, supporting the requested
  pixel format (`YUYV` by default).
- The upstream node publishes `image_raw`; this service's fixed invocation
  remaps it to `/image` (`-r image_raw:=image`), so camera info is published on
  `/camera_info`.

## Limitations and known failure modes

- Raw images are large; sending them over Wi-Fi to another host can drop frames
  or saturate the link.
- The image publisher is reliable; a best-effort image stream would be a
  separate service.
- Image frames carry `frame_id: camera`; the TurtleBot3 URDF has no such frame.

## Configuration notes

Defaults kept: `image_size [640, 480]`, `pixel_format YUYV`, `output_encoding rgb8`,
`camera_frame_id camera`.

## Evaluation

None recorded.

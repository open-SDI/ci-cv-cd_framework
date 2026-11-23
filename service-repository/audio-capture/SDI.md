---
schema_version: sdi.service-description/v1
service_id: audio-capture
title: Microphone audio stream for speech recognition on CPU

provenance:
  basis: declared
  source: >-
    From this folder's audio_capture.py, written for SDI, which captures through
    PyAudio 0.2.13 (the noble python3-pyaudio package) and PortAudio.

devices: [microphone]
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
        - python3-pyaudio=0.2.13-1build3
        - ros-jazzy-rclpy=7.1.12-1*
        - ros-jazzy-std-msgs=5.3.8-1*

subscribes: {}

publishes:
  /audio_listener/audio: {type: std_msgs/msg/Int16MultiArray, reliability: reliable, durability: volatile}
---

## Purpose

Streams the robot's microphone as ROS audio messages, so that a speech
recognition service can hear spoken requests and spoken replies.

## Capabilities

- Publishes 16 kHz mono 16-bit audio on `/audio_listener/audio`, 1000 samples
  per message, reliably.

## Assumptions and preconditions

- A microphone is the default input device that PortAudio sees through ALSA.
- A speech recognition service such as `vosk-speech` consumes the stream.

## Limitations and known failure modes

- When capture fails (PyAudio cannot load, no input device, the stream cannot
  open or stops reading), the node logs the failure once and idles. It keeps
  running and publishes nothing, so a missing microphone shows only in its log.
  PortAudio's device probe also prints many ALSA and JACK messages before that
  line.
- No gain control, noise suppression, or voice activity detection; it sends
  every captured sample.
- Raw audio sent to another host travels over the robot's network link.
- In simulation the capture path is never exercised: there is no microphone,
  and the scenario publishes audio on the same topic itself.

## Configuration notes

Fixed at 16000 Hz, one channel, 1000 frames per buffer; there are no
parameters. The image lists its installed packages in
`/usr/share/sdi/installed-packages.txt`.

## Evaluation

None recorded.

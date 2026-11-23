---
schema_version: sdi.service-description/v1
service_id: whisper-audio-listener
title: Microphone audio stream for ros2_whisper

provenance:
  basis: placeholder
  note: >-
    Facts read from ros-ai/ros2_whisper 59b2d732161d77ffe4da9562bbc34d26b5038eee
    (the version OrchestML 0a951cb lists), audio_listener package; it has not been
    built or run on Jazzy or arm64.

implementation:
  name: ros2_whisper
  version: 1.4.0
  repository: https://github.com/ros-ai/ros2_whisper
  revision: 59b2d732161d77ffe4da9562bbc34d26b5038eee

devices: [microphone]
depends_on: []

artifacts:
  jazzy-source:
    architectures: [amd64, arm64]
    host_requirement:
      os: ubuntu-24.04
      ros_distro: jazzy
    route:
      kind: source
      repository: https://github.com/ros-ai/ros2_whisper
      revision: 59b2d732161d77ffe4da9562bbc34d26b5038eee
    invocation: ros2 run audio_listener audio_listener

subscribes: {}

publishes:
  /audio_listener/audio: {type: std_msgs/msg/Int16MultiArray, reliability: best_effort, durability: volatile}
---

## Purpose

Streams audio from the host's microphone as ROS messages for speech recognition
by `whisper-server`.

## Capabilities

- Publishes 16 kHz mono 16-bit audio in buffers of 1000 samples on
  `/audio_listener/audio`.

## Assumptions and preconditions

- A microphone is the host's default input device (PortAudio through PyAudio).
- `pyaudio` and `numpy` are installed by hand; the package declares only test dependencies.

## Limitations and known failure modes

- Raw audio sent to another host travels over the robot's network link.
- The upstream `whisper_bringup` launch file also starts this node; run alone, use
  `ros2 run` as declared.

## Configuration notes

Defaults kept: `rate` 16000, `channels` 1, `frames_per_buffer` 1000.

## Evaluation

None recorded.

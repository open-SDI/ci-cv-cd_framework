---
schema_version: sdi.service-description/v1
service_id: whisper-server
title: Whisper speech-to-text inference on an NVIDIA GPU

provenance:
  basis: placeholder
  note: >-
    Facts read from ros-ai/ros2_whisper 59b2d732161d77ffe4da9562bbc34d26b5038eee
    (the version OrchestML 0a951cb lists), whisper_server, transcript_manager and
    whisper_bringup; it has never been shown to build on Jazzy, arm64 or CUDA 13.

implementation:
  name: ros2_whisper
  version: 1.4.0
  repository: https://github.com/ros-ai/ros2_whisper
  revision: 59b2d732161d77ffe4da9562bbc34d26b5038eee

devices: [nvidia-gpu]
depends_on: []

artifacts:
  jazzy-cuda-source:
    architectures: [amd64, arm64]
    host_requirement:
      os: ubuntu-24.04
      ros_distro: jazzy
    route:
      kind: source
      repository: https://github.com/ros-ai/ros2_whisper
      revision: 59b2d732161d77ffe4da9562bbc34d26b5038eee
    invocation: ros2 launch whisper_bringup bringup.launch.py

subscribes:
  /audio_listener/audio: {type: std_msgs/msg/Int16MultiArray, reliability: best_effort, durability: volatile}

action_servers:
  /whisper/inference: {type: whisper_idl/action/Inference}
---

## Purpose

Transcribes speech from an audio stream with whisper.cpp on an NVIDIA GPU.

## Capabilities

- Buffers audio from `/audio_listener/audio` and runs Whisper inference
  continuously while active.
- Serves `/whisper/inference`: a goal with a maximum duration returns the
  transcriptions heard in that window, with live feedback.
- Publishes a running transcript on `/whisper/transcript_stream`.

## Assumptions and preconditions

- Another service provides `/audio_listener/audio` (normally `whisper-audio-listener`).
- Built with CUDA: `colcon build --cmake-args -DGGML_CUDA=On`, and on a Jetson Orin
  `-DCMAKE_CUDA_ARCHITECTURES=87`. The build fetches whisper.cpp v1.7.2 from GitHub.
- Downloads the `base.en` model with `wget` from Hugging Face on first start into
  `$HOME/.cache/whisper.cpp`, unless it is already there.

## Limitations and known failure modes

- Likely fails to compile on GCC 13 (Ubuntu 24.04) without the unmerged upstream
  fix for `std::swap` on `vector<bool>` (ros2_whisper pull request #20).
- The upstream `bringup.launch.py` also starts the audio listener on the same
  host; this service needs a launch file that starts only the inference container.
- Inference and the action server live in one component container
  (`whisper_container`); the action needs both running.

## Configuration notes

Upstream `whisper_server/config/whisper.yaml`: model `base.en`, English,
4 threads, GPU 0 with flash attention, 20 s audio buffer, 1000 ms inference period.

## Evaluation

None recorded.

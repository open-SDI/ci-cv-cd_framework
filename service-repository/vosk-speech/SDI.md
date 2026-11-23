---
schema_version: sdi.service-description/v1
service_id: vosk-speech
title: Offline English speech recognition with Vosk on CPU

provenance:
  basis: declared
  source: >-
    From this folder's vosk_speech.py, written for SDI, over Vosk 0.3.45
    (cf2560c9f8a49d3d366b433fdabd78c518231bec) and its
    vosk-model-small-en-us-0.15 model.

implementation:
  name: vosk
  version: 0.3.45
  repository: https://github.com/alphacep/vosk-api
  revision: cf2560c9f8a49d3d366b433fdabd78c518231bec

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
        - ros-jazzy-rclpy=7.1.12-1*
        - ros-jazzy-std-msgs=5.3.8-1*
        - python3-cffi=1.16.0-2build1
        - python3-requests=2.31.0+dfsg-1ubuntu1.2
        - python3-tqdm=4.66.2-2
        - python3-srt=3.5.3-1
        - python3-websockets=10.4-1

subscribes:
  /audio_listener/audio: {type: std_msgs/msg/Int16MultiArray, reliability: reliable, durability: volatile}

publishes:
  /speech/text: {type: std_msgs/msg/String, reliability: reliable, durability: volatile}
---

## Purpose

Recognizes spoken English on the robot's CPU, without a network or GPU, and
publishes what was said as text, so that the robot can take spoken requests
and hear spoken replies.

## Capabilities

- Transcribes 16 kHz mono 16-bit audio from `/audio_listener/audio` with Vosk
  and its small US English model.
- Publishes one message on `/speech/text` per utterance: the recognized words,
  lower case and without punctuation. An utterance ends when no audio has
  arrived for about 500 ms.

## Assumptions and preconditions

- Another service provides `/audio_listener/audio` at 16 kHz (normally
  `audio-capture`), and stops sending between utterances. A continuous stream
  is cut into utterances only where Vosk itself detects a pause, and the text
  is published only after the stream stops.
- The audio subscription is reliable, so a best-effort audio publisher such as
  `whisper-audio-listener` cannot feed it.

## Limitations and known failure modes

- The small English model gives no accuracy guarantee: names, noisy rooms, and
  accented or distant speech are often misrecognized.
- The message is sent even when nothing was recognized, as an empty string.
- Recognizes English only, and reports no confidence, speaker, or language.

## Configuration notes

The recipe installs the Vosk 0.3.45 wheel from PyPI, pinned by hash for each
architecture, without its Python dependencies, which come from noble packages.
It downloads `vosk-model-small-en-us-0.15` at build time and checks its sha256.
There are no parameters. The image lists its installed packages in
`/usr/share/sdi/installed-packages.txt`.

## Evaluation

None recorded.

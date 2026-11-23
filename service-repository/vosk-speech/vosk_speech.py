#!/usr/bin/python3
import json
import time

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Int16MultiArray, String
from vosk import KaldiRecognizer, Model

MODEL = "/opt/sdi/share/vosk_speech/model"
RATE = 16000
SILENCE_S = 0.5


class VoskSpeech(Node):
    def __init__(self) -> None:
        super().__init__("vosk_speech")
        self._recognizer = KaldiRecognizer(Model(MODEL), RATE)
        self._texts: list[str] = []
        self._last_audio: float | None = None
        reliable = QoSProfile(
            depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._publisher = self.create_publisher(String, "/speech/text", reliable)
        self.create_subscription(
            Int16MultiArray, "/audio_listener/audio", self._on_audio, reliable
        )
        # Audio arrives at wall-clock pace, so silence is timed on a steady
        # clock; a sim-time timer would also never fire without /clock.
        self.create_timer(
            0.1,
            self._flush_after_silence,
            clock=Clock(clock_type=ClockType.STEADY_TIME),
        )

    def _on_audio(self, message: Int16MultiArray) -> None:
        self._last_audio = time.monotonic()
        if self._recognizer.AcceptWaveform(message.data.tobytes()):
            self._keep(self._recognizer.Result())

    def _flush_after_silence(self) -> None:
        if self._last_audio is None or time.monotonic() - self._last_audio < SILENCE_S:
            return
        self._keep(self._recognizer.FinalResult())
        text = " ".join(self._texts)
        self._publisher.publish(String(data=text))
        self.get_logger().info(f"utterance: {text!r}")
        self._texts = []
        self._last_audio = None

    def _keep(self, result: str) -> None:
        text = json.loads(result)["text"]
        if text:
            self._texts.append(text)


def main() -> None:
    rclpy.init()
    try:
        rclpy.spin(VoskSpeech())
    except (KeyboardInterrupt, ExternalShutdownException):
        pass


if __name__ == "__main__":
    main()

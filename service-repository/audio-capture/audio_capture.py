#!/usr/bin/python3
import threading
from array import array

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.publisher import Publisher
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Int16MultiArray

RATE = 16000
CHUNK = 1000


def capture(node: Node, publisher: Publisher) -> None:
    try:
        import pyaudio

        stream = pyaudio.PyAudio().open(
            format=pyaudio.paInt16,
            channels=1,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )
        node.get_logger().info("capturing 16 kHz mono from the default input device")
        while rclpy.ok():
            samples = stream.read(CHUNK, exception_on_overflow=False)
            publisher.publish(Int16MultiArray(data=array("h", samples)))
    except Exception as error:
        node.get_logger().error(
            f"audio capture failed, idling without audio: {error!r}"
        )


def main() -> None:
    rclpy.init()
    node = Node("audio_capture")
    qos = QoSProfile(
        depth=100,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )
    publisher = node.create_publisher(Int16MultiArray, "/audio_listener/audio", qos)
    threading.Thread(target=capture, args=(node, publisher), daemon=True).start()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass


if __name__ == "__main__":
    main()

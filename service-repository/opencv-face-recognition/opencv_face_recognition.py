#!/usr/bin/python3
import cv2
import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image
from std_msgs.msg import String

SHARE = "/opt/sdi/share/opencv_face_recognition"
DETECTOR = f"{SHARE}/face_detection_yunet_2022mar.onnx"
RECOGNIZER = f"{SHARE}/face_recognition_sface_2021dec.onnx"
# SFace's published cosine threshold for a same-person match.
MATCH_COSINE = 0.363
# YuNet 2022mar raises cv2.error on any input side shorter than this.
MIN_SIDE = 63

TO_BGR = {
    "bgr8": (3, None),
    "rgb8": (3, cv2.COLOR_RGB2BGR),
    "bgra8": (4, cv2.COLOR_BGRA2BGR),
    "rgba8": (4, cv2.COLOR_RGBA2BGR),
    "mono8": (1, cv2.COLOR_GRAY2BGR),
}


class FaceRecognition(Node):
    def __init__(self) -> None:
        super().__init__("opencv_face_recognition")
        self._detector = cv2.FaceDetectorYN.create(DETECTOR, "", (320, 320))
        self._recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER, "")
        self._gallery: dict[str, np.ndarray] = {}
        self._publisher = self.create_publisher(
            String,
            "/face_recognition/name",
            QoSProfile(
                depth=10,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            ),
        )
        self.create_subscription(
            Image,
            "/face_enrollment",
            self._on_enrollment,
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self.create_subscription(
            Image,
            "/image",
            self._on_image,
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            ),
        )

    def _on_enrollment(self, message: Image) -> None:
        name = message.header.frame_id
        features = self._features_largest_first(message)
        if not name or not features:
            self.get_logger().warning(
                f"enrollment ignored: name {name!r}, {len(features)} faces"
            )
            return
        self._gallery[name] = features[0]
        self.get_logger().info(f"enrolled {name!r}")

    def _on_image(self, message: Image) -> None:
        if not self._gallery:
            return
        names = {
            self._match(feature) for feature in self._features_largest_first(message)
        }
        for name in sorted(names - {None}):
            self._publisher.publish(String(data=name))

    def _match(self, feature: np.ndarray) -> str | None:
        scores = {
            name: self._recognizer.match(
                feature, enrolled, cv2.FaceRecognizerSF_FR_COSINE
            )
            for name, enrolled in self._gallery.items()
        }
        best = max(scores, key=scores.__getitem__)
        return best if scores[best] >= MATCH_COSINE else None

    def _features_largest_first(self, message: Image) -> list[np.ndarray]:
        if message.encoding not in TO_BGR:
            self.get_logger().warning(
                f"unsupported image encoding {message.encoding!r}", once=True
            )
            return []
        channels, conversion = TO_BGR[message.encoding]
        if not (
            min(message.height, message.width) >= MIN_SIDE
            and message.step >= message.width * channels
            and len(message.data) == message.height * message.step
        ):
            self.get_logger().warning(
                f"unusable {message.encoding} image dropped: "
                f"{message.width}x{message.height}, step {message.step}, "
                f"{len(message.data)} bytes",
                once=True,
            )
            return []
        rows = np.frombuffer(message.data, np.uint8).reshape(
            message.height, message.step
        )
        image = np.ascontiguousarray(
            rows[:, : message.width * channels].reshape(
                message.height, message.width, channels
            )
        )
        if conversion is not None:
            image = cv2.cvtColor(image, conversion)
        self._detector.setInputSize((message.width, message.height))
        _, faces = self._detector.detect(image)
        if faces is None:
            return []
        faces = sorted(faces, key=lambda face: face[2] * face[3], reverse=True)
        return [
            self._recognizer.feature(self._recognizer.alignCrop(image, face))
            for face in faces
        ]


def main() -> None:
    rclpy.init()
    try:
        rclpy.spin(FaceRecognition())
    except (KeyboardInterrupt, ExternalShutdownException):
        pass


if __name__ == "__main__":
    main()

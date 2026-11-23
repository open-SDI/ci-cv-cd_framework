"""Count the composition's outputs and fly the S-04 mission, then write the result."""

import array
import importlib
import json
import math
import os
import struct
import time
import wave
import zlib
from collections.abc import Callable
from pathlib import Path

import rclpy
import yaml
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from sensor_msgs.msg import Image
from std_msgs.msg import Int16MultiArray, String
from tf2_ros import Buffer, TransformListener

SCENARIO = Path("/opt/sdi/cv/s-04")
RESULT = Path("/cv/result.json")
SETUP_TIMEOUT_S = 300.0
SPEECH_TIMEOUT_S = 60.0
NAVIGATION_TIMEOUT_S = 480.0
FACE_TIMEOUT_S = 120.0
STATE_CALL_TIMEOUT_S = 5.0
POSE_PERIOD_S = 1.0
DISCOVERY_S = 3.0
SPIN_S = 0.05
CHUNK_SAMPLES = 1000
AUDIO = "/audio_listener/audio"
SPEECH = "/speech/text"
FACE = "/face_recognition/name"
NAVIGATE = "/navigate_to_pose"
LOCALIZED = "tf map->base_footprint"
NAVIGATOR_STATE = "/bt_navigator/get_state"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
COUNTING_QOS = QoSProfile(
    depth=10,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)


def load_type(name: str) -> type:
    """Import a message type named as pkg/msg/Type."""
    package, kind, message = name.split("/")
    if kind != "msg":
        raise ValueError(name)
    return getattr(importlib.import_module(f"{package}.msg"), message)


def read_rgb_png(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB, non-interlaced PNG into rgb8 rows."""
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError(path)
    position, header, compressed = len(PNG_SIGNATURE), b"", b""
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        position += length + 12
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            compressed += body
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    if (depth, colour, interlace) != (8, 2, 0):
        raise ValueError(path)
    raw = zlib.decompress(compressed)
    stride = width * 3
    pixels = bytearray()
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        line = unfilter(
            raw[start], bytearray(raw[start + 1 : start + 1 + stride]), previous
        )
        pixels += line
        previous = line
    return width, height, bytes(pixels)


def unfilter(kind: int, line: bytearray, previous: bytearray) -> bytearray:
    """Undo one PNG scanline filter, three bytes per pixel."""
    for i, value in enumerate(line):
        left = line[i - 3] if i >= 3 else 0  # noqa: PLR2004
        up = previous[i]
        up_left = previous[i - 3] if i >= 3 else 0  # noqa: PLR2004
        predictor = {
            0: 0,
            1: left,
            2: up,
            3: (left + up) // 2,
            4: paeth(left, up, up_left),
        }[kind]
        line[i] = (value + predictor) & 0xFF
    return line


def paeth(left: int, up: int, up_left: int) -> int:
    """Return the PNG Paeth predictor."""
    estimate = left + up - up_left
    distances = (abs(estimate - left), abs(estimate - up), abs(estimate - up_left))
    return (left, up, up_left)[distances.index(min(distances))]


def stamp_pose(pose: dict, message: PoseStamped | PoseWithCovarianceStamped) -> None:
    """Set a map-frame pose on a stamped message from a scenario pose."""
    target = (
        message.pose.pose
        if isinstance(message, PoseWithCovarianceStamped)
        else message.pose
    )
    message.header.frame_id = "map"
    target.position.x = float(pose["x"])
    target.position.y = float(pose["y"])
    target.orientation.z = math.sin(pose["yaw"] / 2)
    target.orientation.w = math.cos(pose["yaw"] / 2)


def graph(node: Node) -> list[dict]:
    """Return every topic in the graph with its types and publisher nodes."""
    return [
        {
            "topic": topic,
            "types": sorted(types),
            "publishers": sorted(
                f"{info.node_namespace.rstrip('/')}/{info.node_name}"
                for info in node.get_publishers_info_by_topic(topic)
            ),
        }
        for topic, types in sorted(node.get_topic_names_and_types())
    ]


class Orchestrator(Node):
    """The mission's node: counting, gate subscriptions, and the mission clients."""

    def __init__(self, counting: list[dict], types: dict[str, type]) -> None:
        """Subscribe to the counting and gate topics; create the mission clients."""
        super().__init__(
            "cv_orchestrator",
            parameter_overrides=[
                Parameter("use_sim_time", Parameter.Type.BOOL, value=True)
            ],
        )
        self.scenario = yaml.safe_load((SCENARIO / "scenario.yaml").read_text())
        self.counts = {item["topic"]: 0 for item in counting}
        self.heard: dict[str, list[float]] = {SPEECH: [], FACE: []}
        for item in counting:
            self.create_subscription(
                types[item["type"]],
                item["topic"],
                self.counter(item["topic"]),
                COUNTING_QOS,
            )
        for topic in self.heard:
            self.create_subscription(String, topic, self.listener(topic), COUNTING_QOS)
        reliable = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE)
        self.audio = self.create_publisher(Int16MultiArray, AUDIO, reliable)
        self.initial_pose = self.create_publisher(
            PoseWithCovarianceStamped, "/initialpose", 10
        )
        self.enrollment = self.create_publisher(
            Image,
            "/face_enrollment",
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self.navigator = ActionClient(self, NavigateToPose, NAVIGATE)
        self.navigator_state = self.create_client(GetState, NAVIGATOR_STATE)
        self.tf = Buffer()
        self.tf_listener = TransformListener(self.tf, self)

    def counter(self, topic: str) -> Callable[[object], None]:
        """Return a callback that counts one topic's messages."""

        def count(_: object) -> None:
            self.counts[topic] += 1

        return count

    def listener(self, topic: str) -> Callable[[object], None]:
        """Return a callback that records when a gate topic's messages arrive."""

        def hear(_: object) -> None:
            self.heard[topic].append(time.monotonic())

        return hear

    def heard_since(self, topic: str, since: float) -> bool:
        """Say whether a gate topic had a message at or after a monotonic time."""
        return any(arrival >= since for arrival in self.heard[topic])

    def spin_until(self, ready: Callable[[], bool], deadline: float) -> bool:
        """Spin until ready() holds or the monotonic deadline passes."""
        while time.monotonic() < deadline:
            if ready():
                return True
            rclpy.spin_once(self, timeout_sec=SPIN_S)
        return ready()


class Mission:
    """S-04's setup and eight steps, each bounded by its own timeout."""

    def __init__(self, node: Orchestrator) -> None:
        """Bind the mission to the node and the scenario's poses and clips."""
        self.node = node
        self.scenario = node.scenario
        self.stop: dict | None = None
        self.arrived_at = 0.0
        self.steps: list[tuple[str, Callable[[], str] | None, str | None]] = [
            ("secure-book", None, None),
            ("accept-spoken-request", lambda: self.speak("request"), SPEECH),
            ("resolve-rendezvous", self.resolve, None),
            ("navigate-to-rendezvous", self.go_to_stop, NAVIGATE),
            ("verify-recipient-face", self.see_face, FACE),
            ("transfer-book", None, None),
            ("acknowledge-receipt", lambda: self.speak("receipt"), SPEECH),
            (
                "return-to-origin",
                lambda: self.navigate(self.scenario["origin"]),
                NAVIGATE,
            ),
        ]

    def run(self) -> dict:
        """Run setup and the steps; the first step that does not pass ends it."""
        setup = self.setup()
        self.log(f"setup {setup['status']} after {setup['elapsed_s']} s")
        going = setup["status"] == "passed"
        steps = []
        for requirement_id, action, waited_on in self.steps:
            step: dict = {"requirement_id": requirement_id}
            if waited_on is not None:
                step["waited_on"] = waited_on
            if not going:
                step["status"] = "not_reached"
            elif waited_on is None:
                if action is not None:
                    action()
                step["status"] = "scripted"
            else:
                started = time.monotonic()
                step["status"] = action()
                step["elapsed_s"] = round(time.monotonic() - started, 1)
                going = step["status"] == "passed"
            self.log(
                f"step {requirement_id} {step['status']} {step.get('elapsed_s', '')}"
            )
            steps.append(step)
        return {"setup": setup, "steps": steps}

    def setup(self) -> dict:
        """Enroll Joe, localize at the origin, and wait for Nav2's navigator."""
        started = time.monotonic()
        deadline = started + SETUP_TIMEOUT_S
        self.enroll()
        next_pose = 0.0

        def localized() -> bool:
            nonlocal next_pose
            if time.monotonic() >= next_pose:
                message = PoseWithCovarianceStamped()
                stamp_pose(self.scenario["origin"], message)
                message.header.stamp = self.node.get_clock().now().to_msg()
                message.pose.covariance[0] = message.pose.covariance[7] = 0.25
                message.pose.covariance[35] = 0.0685
                self.node.initial_pose.publish(message)
                next_pose = time.monotonic() + POSE_PERIOD_S
            return self.node.tf.can_transform("map", "base_footprint", Time())

        waited_on = None
        if not self.node.spin_until(localized, deadline):
            waited_on = LOCALIZED
        elif not self.navigator_active(deadline):
            waited_on = NAVIGATOR_STATE
        setup = {
            "status": "passed" if waited_on is None else "timed_out",
            "elapsed_s": round(time.monotonic() - started, 1),
        }
        if waited_on is not None:
            setup["waited_on"] = waited_on
        return setup

    def enroll(self) -> None:
        """Publish Joe's photo, latched for the whole run."""
        width, height, pixels = read_rgb_png(SCENARIO / self.scenario["joe"]["photo"])
        message = Image()
        message.header.frame_id = "joe"
        message.height, message.width = height, width
        message.encoding, message.step = "rgb8", width * 3
        message.data = pixels
        self.node.enrollment.publish(message)

    def navigator_active(self, deadline: float) -> bool:
        """Poll bt_navigator's lifecycle state, bounding every call."""
        client = self.node.navigator_state
        while time.monotonic() < deadline:
            if client.service_is_ready():
                future = client.call_async(GetState.Request())
                call_deadline = min(deadline, time.monotonic() + STATE_CALL_TIMEOUT_S)
                if self.node.spin_until(future.done, call_deadline):
                    if future.result().current_state.label == "active":
                        return True
                else:
                    client.remove_pending_request(future)
            self.node.spin_until(lambda: False, min(deadline, time.monotonic() + 1.0))
        return False

    def speak(self, clip: str) -> str:
        """Inject a clip at real-time pace and wait for speech heard after it began."""
        deadline = time.monotonic() + SPEECH_TIMEOUT_S
        node = self.node
        if not node.spin_until(
            lambda: node.audio.get_subscription_count() > 0, deadline
        ):
            return "timed_out"
        with wave.open(str(SCENARIO / self.scenario["clips"][clip]), "rb") as audio:
            rate = audio.getframerate()
            samples = array.array("h", audio.readframes(audio.getnframes()))
        began = time.monotonic()
        for index, offset in enumerate(range(0, len(samples), CHUNK_SAMPLES)):
            due = began + index * CHUNK_SAMPLES / rate
            node.spin_until(lambda due=due: time.monotonic() >= due, deadline)
            if time.monotonic() >= deadline:
                return "timed_out"
            message = Int16MultiArray()
            message.data = samples[offset : offset + CHUNK_SAMPLES]
            node.audio.publish(message)
        heard = node.spin_until(lambda: node.heard_since(SPEECH, began), deadline)
        return "passed" if heard else "timed_out"

    def resolve(self) -> None:
        """Look up Joe's stop in the rendezvous mapping."""
        self.stop = self.scenario["poses"][self.scenario["rendezvous"]["joe"]]

    def go_to_stop(self) -> str:
        """Navigate to Joe's stop and remember when the robot arrived."""
        status = self.navigate(self.stop)
        self.arrived_at = time.monotonic()
        return status

    def see_face(self) -> str:
        """Wait for a recognized face after the robot arrived."""
        deadline = time.monotonic() + FACE_TIMEOUT_S
        seen = self.node.spin_until(
            lambda: self.node.heard_since(FACE, self.arrived_at), deadline
        )
        return "passed" if seen else "timed_out"

    def navigate(self, pose: dict) -> str:
        """Send one NavigateToPose goal and wait for it to finish."""
        deadline = time.monotonic() + NAVIGATION_TIMEOUT_S
        node = self.node
        if not node.spin_until(node.navigator.server_is_ready, deadline):
            return "timed_out"
        goal = NavigateToPose.Goal()
        stamp_pose(pose, goal.pose)
        goal.pose.header.stamp = node.get_clock().now().to_msg()
        sent = node.navigator.send_goal_async(goal)
        if not node.spin_until(sent.done, deadline):
            return "timed_out"
        handle = sent.result()
        if not handle.accepted:
            return "failed"
        finished = handle.get_result_async()
        if not node.spin_until(finished.done, deadline):
            cancelled = handle.cancel_goal_async()
            node.spin_until(cancelled.done, time.monotonic() + STATE_CALL_TIMEOUT_S)
            return "timed_out"
        return (
            "passed"
            if finished.result().status == GoalStatus.STATUS_SUCCEEDED
            else "failed"
        )

    def log(self, text: str) -> None:
        """Log one progress line."""
        self.node.get_logger().info(text)


def write_result(result: dict) -> None:
    """Write the result file by temporary file and rename."""
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    temporary = RESULT.with_name(".result.json.tmp")
    temporary.write_text(json.dumps(result, separators=(",", ":")))
    temporary.replace(RESULT)


def main() -> None:
    """Load the counting types, then count and fly the mission."""
    counting = json.loads(os.environ["CV_COUNTING"])
    types: dict[str, type] = {}
    unloadable = []
    for item in counting:
        try:
            types[item["type"]] = load_type(item["type"])
        except (ValueError, ImportError, AttributeError):
            unloadable.append({"topic": item["topic"], "type": item["type"]})
    rclpy.init()
    if unloadable:
        node = Node("cv_orchestrator")
        node.get_logger().error(f"unloadable counting types: {unloadable}")
        deadline = time.monotonic() + DISCOVERY_S
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=SPIN_S)
        write_result(
            {"unloadable_types": unloadable, "counts": {}, "graph": graph(node)}
        )
        return
    node = Orchestrator(counting, types)
    for item in counting:
        node.get_logger().info(
            f"counting {item['topic']} ({item['type']}) for {item['service_id']}"
        )
    mission = Mission(node).run()
    write_result(
        {
            "unloadable_types": [],
            "counts": node.counts,
            "mission": mission,
            "graph": graph(node),
        }
    )


if __name__ == "__main__":
    main()

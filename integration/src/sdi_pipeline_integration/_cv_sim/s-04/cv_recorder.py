"""Record the overview camera to an mp4 while the sim runs.

cv_recorder.py run   subscribe to the camera and stream its frames to ffmpeg
cv_recorder.py stop  end the recording and wait until the mp4 is written

`run` starts with the sim when CV_RECORDING=1. CV calls `stop` after the
mission, so the recording never affects the verdict. `stop` exits 0 only when
the mp4 exists and prints one summary line either way.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

TOPIC = "/sdi/overview"
WIDTH, HEIGHT, FPS = 640, 480, 10
FOLDER = Path("/cv/recording")
VIDEO = FOLDER / "sim-recording.mp4"
PARTIAL = FOLDER / "partial.mp4"
PID = FOLDER / "recorder.pid"
SUMMARY = FOLDER / "summary.txt"
STOP_POLL_S = 0.2


def run() -> None:
    """Stream every camera frame to ffmpeg until SIGTERM or SIGINT."""
    from gz.msgs10.image_pb2 import Image  # noqa: PLC0415
    from gz.transport13 import Node  # noqa: PLC0415

    FOLDER.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{WIDTH}x{HEIGHT}",
            "-framerate",
            str(FPS),
            "-i",
            "pipe:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "26",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(PARTIAL),
        ],
        stdin=subprocess.PIPE,
    )
    lock = threading.Lock()
    stopped = threading.Event()
    tally = {"frames": 0, "skipped": 0}

    def on_image(message: Image) -> None:
        with lock:
            if stopped.is_set():
                return
            if (message.width, message.height) != (WIDTH, HEIGHT) or len(
                message.data
            ) != WIDTH * HEIGHT * 3:
                tally["skipped"] += 1
                return
            try:
                encoder.stdin.write(message.data)
            except OSError:
                tally["skipped"] += 1
                return
            tally["frames"] += 1

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    node = Node()
    subscribed = node.subscribe(Image, TOPIC, on_image)
    PID.write_text(str(os.getpid()))
    stopped.wait()
    with lock:
        encoder.stdin.close()
    code = encoder.wait()
    if code == 0 and tally["frames"] > 0:
        PARTIAL.replace(VIDEO)
    SUMMARY.write_text(
        f"subscribed {subscribed} frames {tally['frames']} "
        f"skipped {tally['skipped']} ffmpeg exit {code}\n"
    )


def stop() -> int:
    """Signal the recorder and wait for its summary; exit 0 if the mp4 exists."""
    try:
        pid = int(PID.read_text())
    except (OSError, ValueError):
        sys.stdout.write("recorder never started\n")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
        while not SUMMARY.exists():
            time.sleep(STOP_POLL_S)
            os.kill(pid, 0)
    except ProcessLookupError:
        if not SUMMARY.exists():
            sys.stdout.write("recorder exited before writing its summary\n")
            return 1
    sys.stdout.write(SUMMARY.read_text())
    return 0 if VIDEO.is_file() else 1


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["run"]:
            run()
        case ["stop"]:
            sys.exit(stop())
        case _:
            sys.exit(__doc__)

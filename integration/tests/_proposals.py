"""A known-good composition proposal for the Waffle-Orin run."""

from __future__ import annotations

from typing import Any

NAVIGATION = ["nav2-navigation", "nav2-localization", "turtlebot3-bringup"]
SPEECH = ["whisper-audio-listener", "whisper-server"]
COMPLETE: dict[str, Any] = {
    "rationale": "Navigation and speech on their natural hosts.",
    "services": [
        {
            "service_id": service_id,
            "artifact_id": artifact_id,
            "host": host,
            "capability": capability,
        }
        for service_id, artifact_id, host, capability in [
            ("turtlebot3-bringup", "jazzy-debs", "waffle", "base and lidar"),
            ("nav2-localization", "jazzy-debs", "orin", "localization"),
            ("nav2-navigation", "jazzy-debs", "orin", "path planning"),
            ("v4l2-camera", "jazzy-debs", "waffle", "camera images"),
            ("face-recog", "jazzy-source", "orin", "face recognition"),
            ("whisper-audio-listener", "jazzy-source", "waffle", "audio capture"),
            ("whisper-server", "jazzy-cuda-source", "orin", "speech to text"),
        ]
    ],
    "coverage": [
        {
            "requirement_id": requirement_id,
            "status": status,
            "services": services,
            "reason": "Test-only claim.",
        }
        for requirement_id, status, services in [
            ("secure-book", "outside_composition", []),
            ("accept-spoken-request", "supported", SPEECH),
            ("resolve-rendezvous", "uncertain", ["whisper-server"]),
            ("navigate-to-rendezvous", "supported", NAVIGATION),
            ("verify-recipient-face", "supported", ["face-recog", "v4l2-camera"]),
            ("transfer-book", "outside_composition", []),
            ("acknowledge-receipt", "supported", SPEECH),
            ("return-to-origin", "supported", NAVIGATION),
        ]
    ],
}

"""CV's topic constant is exactly what the scenario machinery publishes."""

from __future__ import annotations

import pytest

from sdi_pipeline_integration._cv_verdict import TOPIC_CONSTANT, topic_constant_drift
from tests._cv_recordings import NAMES, recorded


def test_cv_topic_constant_holds_the_fifteen_machinery_topics() -> None:
    assert sorted(TOPIC_CONSTANT) == [
        "/audio_listener/audio",
        "/camera_info",
        "/clock",
        "/face_enrollment",
        "/image",
        "/imu",
        "/initialpose",
        "/joint_states",
        "/odom",
        "/parameter_events",
        "/robot_description",
        "/rosout",
        "/scan",
        "/tf",
        "/tf_static",
    ]


@pytest.mark.parametrize("name", NAMES)
def test_cv_topic_constant_matches_each_recorded_graph(name: str) -> None:
    assert topic_constant_drift(recorded(name).graph) == (frozenset(), frozenset())

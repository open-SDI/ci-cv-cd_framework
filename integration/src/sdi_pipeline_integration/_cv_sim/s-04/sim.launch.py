"""Run the S-04 sim: headless Gazebo, the ROS bridge, and robot_state_publisher.

The CV_ROBOT environment variable picks the robot.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import ExecuteProcess, Shutdown
from launch_ros.actions import Node

ROBOTS = {
    "waffle_pi": "turtlebot3_waffle_pi",
    "burger_cam": "turtlebot3_burger_cam",
}
SCENARIO_DIR = Path(__file__).parent
WORLD_XACRO = "/opt/ros/jazzy/share/nav2_minimal_tb3_sim/worlds/tb3_sandbox.sdf.xacro"


def robot_model() -> str:
    """Return the turtlebot3_gazebo model that CV_ROBOT names."""
    name = os.environ.get("CV_ROBOT", "")
    if name not in ROBOTS:
        message = f"CV_ROBOT must be one of {', '.join(ROBOTS)}, not {name!r}"
        raise ValueError(message)
    return ROBOTS[name]


def world_sdf(model: str, scenario: dict) -> str:
    """Return tb3_sandbox with the robot and Joe's poster added."""
    world = subprocess.run(
        ["xacro", WORLD_XACRO, "headless:=true"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    origin = scenario["origin"]
    poster = scenario["joe"]["poster"]
    side = poster["face_width_m"] / scenario["joe"]["photo_face_fraction"]
    photo = SCENARIO_DIR / scenario["joe"]["photo"]
    additions = f"""
    <include>
      <uri>model://{model}</uri>
      <name>robot</name>
      <pose>{origin["x"]} {origin["y"]} 0.01 0 0 {origin["yaw"]}</pose>
    </include>
    <model name="joe_poster">
      <static>true</static>
      <pose>{poster["x"]} {poster["y"]} 0.01 0 0 0</pose>
      <link name="link">
        <collision name="collision">
          <pose>0.005 0 {side / 2} 0 0 0</pose>
          <geometry><box><size>0.01 {side} {side}</size></box></geometry>
        </collision>
        <visual name="visual">
          <geometry><mesh><uri>{SCENARIO_DIR}/joe/poster.obj</uri><scale>{side} {side} {side}</scale></mesh></geometry>
          <material>
            <ambient>1 1 1 1</ambient>
            <diffuse>1 1 1 1</diffuse>
            <specular>0 0 0 1</specular>
            <emissive>1 1 1 1</emissive>
            <pbr><metal><albedo_map>{photo}</albedo_map><emissive_map>{photo}</emissive_map><metalness>0</metalness><roughness>1</roughness></metal></pbr>
          </material>
        </visual>
      </link>
    </model>
"""  # noqa: E501
    return world.replace("</world>", additions + "</world>", 1)


def generate_launch_description() -> LaunchDescription:
    """Start the sim; the container exits when any of its processes does."""
    model = robot_model()
    scenario = yaml.safe_load((SCENARIO_DIR / "scenario.yaml").read_text())
    with tempfile.NamedTemporaryFile(
        "w", prefix="s-04-world-", suffix=".sdf", delete=False
    ) as world:
        world.write(world_sdf(model, scenario))
    urdf_path = Path(
        get_package_share_directory("turtlebot3_gazebo"), "urdf", f"{model}.urdf"
    )
    return LaunchDescription(
        [
            ExecuteProcess(
                cmd=["gz", "sim", "-r", "-s", "-v", "2", world.name],
                output="screen",
                on_exit=Shutdown(),
            ),
            Node(
                package="ros_gz_bridge",
                executable="parameter_bridge",
                name="s04_bridge",
                parameters=[{"config_file": str(SCENARIO_DIR / "bridge.yaml")}],
                output="screen",
                on_exit=Shutdown(),
            ),
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                parameters=[
                    {"use_sim_time": True, "robot_description": urdf_path.read_text()}
                ],
                output="screen",
                on_exit=Shutdown(),
            ),
        ]
    )

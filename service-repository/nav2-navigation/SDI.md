---
schema_version: sdi.service-description/v1
service_id: nav2-navigation
title: Nav2 navigation stack with TurtleBot3 Waffle Pi parameters

provenance:
  basis: declared
  source: >-
    From Nav2 1.3.13 (f4108e5b1c2bce804a1aa0c7be6673a8eb4a1501)
    nav2_bringup/launch/navigation_launch.py and TurtleBot3 2.3.6
    (da785b7201d317e6e2a662e41bb3d3fd50ebd503) turtlebot3_navigation2/param/waffle_pi.yaml.

implementation:
  name: navigation2
  version: 1.3.13
  repository: https://github.com/ros-navigation/navigation2
  revision: f4108e5b1c2bce804a1aa0c7be6673a8eb4a1501

devices: []
depends_on: []

artifacts:
  jazzy-debs:
    architectures: [amd64, arm64]
    host_requirement:
      os: ubuntu-24.04
      ros_distro: jazzy
    route:
      kind: apt
      packages:
        - ros-jazzy-nav2-bringup=1.3.13-1*
        - ros-jazzy-navigation2=1.3.13-1*
        - ros-jazzy-turtlebot3-navigation2=2.3.6-1*
    invocation: >-
      ros2 launch nav2_bringup navigation_launch.py use_sim_time:=false
      params_file:=/opt/ros/jazzy/share/turtlebot3_navigation2/param/waffle_pi.yaml

subscribes:
  /map:       {type: nav_msgs/msg/OccupancyGrid, reliability: reliable, durability: transient_local}
  /scan:      {type: sensor_msgs/msg/LaserScan, reliability: best_effort, durability: volatile}
  /odom:      {type: nav_msgs/msg/Odometry, reliability: reliable, durability: volatile}
  /tf:        {type: tf2_msgs/msg/TFMessage}
  /tf_static: {type: tf2_msgs/msg/TFMessage, reliability: reliable, durability: transient_local}

publishes:
  /cmd_vel: {type: geometry_msgs/msg/TwistStamped, reliability: reliable, durability: volatile}

action_servers:
  /navigate_to_pose: {type: nav2_msgs/action/NavigateToPose}

frames:
  global: map
  odometry: odom
  robot_base: base_link
---

## Purpose

Plans and follows a path to a goal pose on a known 2D occupancy map, avoiding
obstacles seen by a planar lidar. Tuned for a TurtleBot3 Waffle Pi.

## Capabilities

- Drives the robot to a goal pose sent as a `navigate_to_pose` action, given a
  map, odometry, a laser scan, and a `map → odom` transform.
- Recovers from blocked paths by spinning, backing up, or waiting.
- Stops or slows near obstacles through its collision monitor, independently of
  the planner.

## Assumptions and preconditions

- Another service provides `/map` and the `map → odom` transform (normally
  `nav2-localization`). This service does not localise and ignores `/initialpose`.
- The robot base accepts `TwistStamped` on `/cmd_vel`, as TurtleBot3 Jazzy bringup does.

## Limitations and known failure modes

- Fails to activate if the `map → odom → base_link` transforms do not arrive.
- Lists only the endpoints the target navigation run uses; Nav2 also offers other
  goal inputs, teleoperation, docking, route graphs, and debug topics.
- Docking is configured for Nova Carter docks and will not work on the Waffle Pi.

## Configuration notes

Uses the TurtleBot3 parameter file installed by `ros-jazzy-turtlebot3-navigation2`
unchanged. It sets `enable_stamped_cmd_vel: true` on every velocity node, a 0.15 m
robot radius, and the DWB controller.

## Evaluation

None recorded.

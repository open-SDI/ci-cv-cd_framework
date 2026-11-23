---
schema_version: sdi.service-description/v1
service_id: turtlebot3-bringup
title: TurtleBot3 Waffle Pi onboard bringup

provenance:
  basis: declared
  source: >-
    From TurtleBot3 2.3.6 (da785b7201d317e6e2a662e41bb3d3fd50ebd503)
    turtlebot3_bringup/launch/robot.launch.py, turtlebot3_bringup/param/waffle_pi.yaml
    and turtlebot3_node, with the hls_lfcd_lds_driver and ld08_driver Jazzy releases.

implementation:
  name: turtlebot3
  version: 2.3.6
  repository: https://github.com/ROBOTIS-GIT/turtlebot3
  revision: da785b7201d317e6e2a662e41bb3d3fd50ebd503

devices: [opencr, lidar]
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
        - ros-jazzy-turtlebot3-bringup=2.3.6-1*
        - ros-jazzy-turtlebot3-node=2.3.6-1*
        - ros-jazzy-turtlebot3-description=2.3.6-1*
        - ros-jazzy-hls-lfcd-lds-driver=2.1.1-1*
        - ros-jazzy-ld08-driver=1.1.4-1*
    invocation: >-
      TURTLEBOT3_MODEL=waffle_pi LDS_MODEL=LDS-01
      ros2 launch turtlebot3_bringup robot.launch.py usb_port:=/dev/ttyACM0

subscribes:
  /cmd_vel: {type: geometry_msgs/msg/TwistStamped, reliability: reliable, durability: volatile}

publishes:
  /scan:      {type: sensor_msgs/msg/LaserScan, reliability: best_effort, durability: volatile}
  /odom:      {type: nav_msgs/msg/Odometry, reliability: reliable, durability: volatile}
  /tf:        {type: tf2_msgs/msg/TFMessage}
  /tf_static: {type: tf2_msgs/msg/TFMessage, reliability: reliable, durability: transient_local}

frames:
  odometry: odom
  robot_base: base_footprint
---

## Purpose

Brings up a TurtleBot3 Waffle Pi's own hardware: drives the wheels through the
OpenCR board, publishes wheel odometry and the planar lidar scan, and publishes
the robot's fixed transforms from its URDF.

## Capabilities

- Moves the robot from velocity commands on `/cmd_vel`.
- Publishes `/odom` and the `odom → base_footprint` transform from the wheels and IMU.
- Publishes `/scan` from the LDS lidar in the `base_scan` frame.
- Publishes the URDF's fixed transforms (`base_footprint → base_link → base_scan`,
  `imu_link`) on `/tf_static` through `robot_state_publisher`.

## Assumptions and preconditions

- Runs on the robot itself: OpenCR on `/dev/ttyACM0` and the lidar on `/dev/ttyUSB0`.
- `TURTLEBOT3_MODEL` and `LDS_MODEL` must be set; the launch file fails without them.
  `LDS-01` selects the `hls_lfcd_lds_driver`; a robot with the newer LDS-02 needs `LDS_MODEL=LDS-02`.
- Velocity commands must be `TwistStamped`: the shipped Waffle Pi parameter file
  sets `enable_stamped_cmd_vel: true`.

## Limitations and known failure modes

- The odometry transform and the stamped `/cmd_vel` come from the shipped
  parameter file; a replacement file that omits `odometry.publish_tf` or
  `enable_stamped_cmd_vel` silently drops the transform or switches to `Twist`.
- Lists only the endpoints the target navigation run uses; the node also offers
  IMU, joint states, battery, sensor state and sound/motor-power services.

## Configuration notes

Uses `turtlebot3_bringup/param/waffle_pi.yaml` unchanged: wheel separation 0.287 m,
wheel radius 0.033 m, odometry frames `odom → base_footprint` with the IMU fused.

## Evaluation

None recorded.

---
schema_version: sdi.service-description/v1
service_id: nav2-localization
title: Nav2 AMCL localization on the TurtleBot3 world map

provenance:
  basis: declared
  source: >-
    From Nav2 1.3.13 (f4108e5b1c2bce804a1aa0c7be6673a8eb4a1501)
    nav2_bringup/launch/localization_launch.py, nav2_amcl and nav2_map_server, and
    TurtleBot3 2.3.6 (da785b7201d317e6e2a662e41bb3d3fd50ebd503)
    turtlebot3_navigation2/param/waffle_pi.yaml and map/map.yaml.

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
        - ros-jazzy-nav2-amcl=1.3.13-1*
        - ros-jazzy-nav2-map-server=1.3.13-1*
        - ros-jazzy-turtlebot3-navigation2=2.3.6-1*
    invocation: >-
      ros2 launch nav2_bringup localization_launch.py use_sim_time:=false
      map:=/opt/ros/jazzy/share/turtlebot3_navigation2/map/map.yaml
      params_file:=/opt/ros/jazzy/share/turtlebot3_navigation2/param/waffle_pi.yaml

subscribes:
  /scan:        {type: sensor_msgs/msg/LaserScan, reliability: best_effort, durability: volatile}
  /initialpose: {type: geometry_msgs/msg/PoseWithCovarianceStamped}
  /tf:          {type: tf2_msgs/msg/TFMessage}
  /tf_static:   {type: tf2_msgs/msg/TFMessage, reliability: reliable, durability: transient_local}

publishes:
  /map: {type: nav_msgs/msg/OccupancyGrid, reliability: reliable, durability: transient_local}
  /tf:  {type: tf2_msgs/msg/TFMessage}

frames:
  global: map
  odometry: odom
  robot_base: base_footprint
---

## Purpose

Serves a known 2D occupancy map and localises the robot on it with AMCL, using
the planar lidar and odometry. Uses the map of the TurtleBot3 simulation world.

## Capabilities

- Publishes the map once on `/map` (latched for late subscribers).
- Estimates the robot pose from `/scan` and the `odom → base_footprint` transform
  and publishes the `map → odom` transform on `/tf`.
- Takes an initial pose estimate on `/initialpose`.

## Assumptions and preconditions

- Another service provides `/scan` and the `odom → base_footprint` transform
  (normally `turtlebot3-bringup`).
- Waits for an initial pose: the parameter file sets `set_initial_pose: false`, so
  nothing publishes `map → odom` until `/initialpose` arrives.

## Limitations and known failure modes

- The map is `turtlebot3_world`, packaged with `turtlebot3_navigation2`; a real
  site needs its own map, which is another service.
- Lists only the endpoints the target navigation run uses; AMCL also publishes
  `/amcl_pose` and `/particle_cloud`, and the map server offers map loading.

## Configuration notes

Uses the TurtleBot3 Waffle Pi parameter file unchanged: AMCL frames `map`, `odom`
and `base_footprint`, scan topic `scan`. The `map:=` argument overrides the
parameter file's map path.

## Evaluation

None recorded.

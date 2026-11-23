# CV builds this image through the Python docker SDK, which has only the
# classic builder, so this file uses no BuildKit features: no `# syntax=`
# line, no RUN --mount, no COPY --chmod, and no heredocs.
FROM ubuntu:noble@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 ROS_DISTRO=jazzy

COPY ros-snapshot-archive-keyring.asc /etc/apt/keyrings/ros-snapshot-archive-keyring.asc

# The bare image has no CA bundle, so the first update skips TLS peer checks;
# apt still verifies every index by signature. Both snapshot hosts return
# bursts of 5xx errors that outlast apt's own retries, and the classic builder
# has no cache mount to keep fetched packages across builds.
RUN retry() { for attempt in 1 2 3 4 5; do "$@" && return 0; sleep 60; done; return 1; } \
 && rm -f /etc/apt/sources.list.d/ubuntu.sources \
 && printf '%s\n' 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/90sdi \
 && printf '%s\n' \
    'Types: deb' \
    'URIs: https://snapshot.ubuntu.com/ubuntu/20261012T164422Z/' \
    'Suites: noble noble-updates noble-security' \
    'Components: main universe' \
    'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
    > /etc/apt/sources.list.d/ubuntu-snapshot.sources \
 && printf '%s\n' \
    'Types: deb' \
    'URIs: http://snapshots.ros.org/jazzy/2026-10-05/ubuntu/' \
    'Suites: noble' \
    'Components: main' \
    'Signed-By: /etc/apt/keyrings/ros-snapshot-archive-keyring.asc' \
    > /etc/apt/sources.list.d/ros-snapshot.sources \
 && retry apt-get -o Acquire::https::Verify-Peer=false update --error-on=any \
 && retry apt-get -o Acquire::https::Verify-Peer=false install -y --no-install-recommends ca-certificates \
 && retry apt-get update --error-on=any \
 && retry apt-get install -y --no-install-recommends \
    ros-jazzy-ros2launch \
    ros-jazzy-ros-gz-sim \
    ros-jazzy-ros-gz-bridge \
    ros-jazzy-nav2-minimal-tb3-sim \
    ros-jazzy-turtlebot3-gazebo \
    ros-jazzy-robot-state-publisher \
    ros-jazzy-xacro \
    ros-jazzy-nav2-msgs \
    ros-jazzy-tf2-ros-py \
    python3-yaml \
    libegl-mesa0 \
    libgl1-mesa-dri \
 && mkdir -p /usr/share/sdi \
 && dpkg-query -W -f='${Package}=${Version}\n' > /usr/share/sdi/installed-packages.txt \
 && rm -rf /var/lib/apt/lists/*

# nav2_minimal_tb3_sim comes first so model://turtlebot3_world resolves to the
# geometry Nav2's map matches.
ENV GZ_SIM_RESOURCE_PATH=/opt/ros/jazzy/share/nav2_minimal_tb3_sim/models:/opt/ros/jazzy/share/turtlebot3_gazebo/models

ENTRYPOINT ["/bin/bash", "-c", "source /opt/ros/jazzy/setup.bash && exec \"$@\"", "--"]

RUN printf '%s\n' '#!/bin/sh' 'exec python3 /opt/sdi/cv/s-04/cv_orchestrator.py' > /usr/local/bin/cv-orchestrator \
 && chmod 755 /usr/local/bin/cv-orchestrator

COPY bridge.yaml /opt/sdi/cv/s-04/
COPY joe/ /opt/sdi/cv/s-04/joe/
COPY clips/request.wav clips/receipt.wav /opt/sdi/cv/s-04/clips/
COPY scenario.yaml sim.launch.py cv_orchestrator.py /opt/sdi/cv/s-04/
WORKDIR /opt/sdi/cv/s-04
CMD ["ros2", "launch", "/opt/sdi/cv/s-04/sim.launch.py"]

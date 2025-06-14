### Adding New Worlds to Gazebo
Assuming default file structure from the turtlebot3 manual:
1. Add your world file to `turtlebot3_ws/src/turtlebot3_simulations/turtlebot3_gazebo/worlds`
2. Add your models to `turtlebot3_ws/src/turtlebot3_simulations/turtlebot3_gazebo/models`
3. Add your launch file to `turtlebot3_ws/src/turtlebot3_simulations/turtlebot3_gazebo/launch`
3. Change directory to `turtlebot3_ws` and build with `colcon build --symlink-install`
4. Launch gazebo sim with `ros2 launch turtlebot3_gazebo <launch_file_name>.launch.py`
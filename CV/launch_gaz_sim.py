import sys
import yaml
import subprocess
import json
from pathlib import Path



# ros 프레임워크로 구현된 nav2 service를 gazebo simulation 환경에서 testing


def load_yaml(yaml_path):
    try:
        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)
        return data
    except Exception as e:
        print(f"[Error] Failed to load YAML file: {e}")
        sys.exit(1)

def find_navigation_service(tasks): # navigate service만 gazebo로 testing 
    for task_entry in tasks:
        if task_entry.get('task') == 'navigate':
            return task_entry.get('service')
    return None

def run_gazebo_simulation(service_name):
    print(f"[INFO] Launching Gazebo simulation for service: {service_name}")
    result = {
        "task": "navigate",
        "service": service_name,
        "success": False  # 기본 실패로 시작
    }

    # try:
    #     # 실제 환경에 맞게 조정: 시뮬레이션 실행
    #     subprocess.run(['ros2', 'launch', service_name, 'sim_launch.py'], check=True)
    #     result["success"] = True
    #     print("[INFO] Simulation completed successfully.")
    # except subprocess.CalledProcessError as e:
    #     print(f"[ERROR] Simulation failed: {e}")
    #     result["success"] = False

    # 결과 저장
    save_results(result)

def save_results(result_dict):
    output_path = Path("cv_results.json")
    try:
        with output_path.open('w') as f:
            json.dump(result_dict, f, indent=2)
        print(f"[INFO] Results saved to {output_path}")
    except Exception as e:
        print(f"[ERROR] Failed to save results: {e}")

def main():
    if len(sys.argv) != 2:
        print("Usage: python launch_gaz_sim.py composition_plan.yaml")
        sys.exit(1)

    yaml_path = sys.argv[1]
    data = load_yaml(yaml_path)

    tasks = data.get('tasks', [])
    nav_service = find_navigation_service(tasks)

    if not nav_service:
        print("[INFO] No 'navigate' task found in YAML.")
        sys.exit(0)

    run_gazebo_simulation(nav_service)

if __name__ == "__main__":
    main()
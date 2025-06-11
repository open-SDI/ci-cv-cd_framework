import sys
import yaml

# 태스크와 서비스 이름 매핑....
TASK_SERVICE_MAP = {
    "facial_detection": "bob_facial_detection",
    "navigate": "nav2",
    "speech-to-text": "ros-vosk",
}

def parse_tasks(file_path):
    with open(file_path, 'r') as f:
        lines = f.readlines()
    tasks = [line.strip() for line in lines if line.strip()]
    return tasks


def generate_composition_plan(tasks):
    plan = {"tasks": []}
    for task in tasks:
        service = TASK_SERVICE_MAP.get(task)
        if service:
            plan["tasks"].append({"task": task, "service": service})
        else:
            print(f"Warning: No service mapping for task '{task}'")
    return plan


def write_yaml(data, output_path="composition_plan.yml"):
    with open(output_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python decomposition.py requirements.txt")
        sys.exit(1)

    input_file = sys.argv[1]
    tasks = parse_tasks(input_file)
    plan = generate_composition_plan(tasks)
    write_yaml(plan)
    print(f"Saved plan to composition_plan.yml")
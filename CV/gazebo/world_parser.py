import os
import shutil
import xml.etree.ElementTree as ET

def extract_unique_models(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()

    models = set()
    for uri in root.iter('uri'):
        text = uri.text
        if text and text.startswith("model://"):
            parts = text.split('/')
            if len(parts) >= 3:
                models.add(parts[2])
    return models

def copy_model_folders(model_names, source_dir, target_dir):
    os.makedirs(target_dir, exist_ok=True)

    for model_name in model_names:
        src_path = os.path.join(source_dir, model_name)
        dst_path = os.path.join(target_dir, model_name)
        if os.path.isdir(src_path):
            shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
            print(f"Copied: {model_name}")
        else:
            print(f"Not found: {model_name}")

xml_file = 'office.world'
source_models_dir = 'models'
target_models_dir = '_models'

models = extract_unique_models(xml_file)
copy_model_folders(models, source_models_dir, target_models_dir)

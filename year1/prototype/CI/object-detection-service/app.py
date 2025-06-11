from fastapi import FastAPI
from transformers import YolosImageProcessor, YolosForObjectDetection
import torch
import os
import cv2
from PIL import Image
import requests

app = FastAPI()

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = YolosForObjectDetection.from_pretrained("hustvl/yolos-tiny").to(device)
image_processor = YolosImageProcessor.from_pretrained("hustvl/yolos-tiny")

SAMPLE_VIDEO_PATH = "/app/sample_video.mp4"

UPSCALER_URL = "http://upscaler-service:8002/upscale/"

# Max number of frames to forward
MAX_FRAMES_TO_FORWARD = 1


def load_video(video_path):
    """
    Load the video from the given path.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Unable to open video file: {video_path}")
    return cap


def extract_frames(video_capture):
    """
    Generator function to extract frames from a video capture object.
    """
    while video_capture.isOpened():
        ret, frame = video_capture.read()
        if not ret:
            break  # End of video
        yield frame
    video_capture.release()


def perform_object_detection(frame):
    """
    Perform object detection on a single frame.
    Returns the detected objects and their metadata.
    """
    # Convert OpenCV frame to PIL image
    pil_image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    # Prepare the input for YOLOS
    inputs = image_processor(images=pil_image, return_tensors="pt").to(device)

    # Perform inference
    with torch.no_grad():
        outputs = model(**inputs)

    # Post-process detection results
    target_sizes = torch.tensor([pil_image.size[::-1]])
    results = image_processor.post_process_object_detection(
        outputs, threshold=0.9, target_sizes=target_sizes
    )[0]

    return results


def filter_and_forward_frame(frame, results, forwarded_count):
    """
    Check detection results for the target label ('truck') and forward the frame if found.
    Returns True if the frame is forwarded; otherwise, False.
    """
    for score, label, box in zip(
        results["scores"], results["labels"], results["boxes"]
    ):
        label_name = model.config.id2label[label.item()]
        if label_name == "truck":  # Modify for your target label
            print(f"Detected {label_name} with score {score.item():.2f}")

            # Forward the frame only if the quota has not been reached
            if forwarded_count < MAX_FRAMES_TO_FORWARD:
                _, encoded_image = cv2.imencode(".jpg", frame)
                response = requests.post(
                    UPSCALER_URL,
                    files={"file": ("frame.jpg", encoded_image.tobytes())},
                )
                print(
                    f"Super-Resolution Service response: {response.status_code}, {response.json()}"
                )
                return True
    return False


### MAIN ENDPOINT ###


@app.post("/start_pipeline/")
async def start_pipeline():
    """
    Start processing the sample video frame-by-frame for object detection.
    Forward only 3 frames with the target label to the Super-Resolution Service.
    """
    try:
        cap = load_video(SAMPLE_VIDEO_PATH)
    except (FileNotFoundError, ValueError) as e:
        return {"error": str(e)}

    frame_count = 0
    forwarded_frames = 0

    for frame in extract_frames(cap):
        frame_count += 1

        # Perform object detection
        results = perform_object_detection(frame)

        # Filter and forward relevant frames
        if forwarded_frames < MAX_FRAMES_TO_FORWARD:
            if filter_and_forward_frame(frame, results, forwarded_frames):
                forwarded_frames += 1

    return {
        "status": "processing_complete",
        "total_frames": frame_count,
        "forwarded_frames": forwarded_frames,
        "max_frames_to_forward": MAX_FRAMES_TO_FORWARD,
    }

@app.post("/notify/")
async def notify(data: dict):
    """
    Handle notifications from the classification service.
    """
    print(f"Notification received: {data}")
    return {"status": "success", "message": "Notification received."}
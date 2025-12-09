from fastapi import FastAPI, UploadFile, File
from transformers import AutoImageProcessor, ResNetForImageClassification
import torch
from PIL import Image
from io import BytesIO
import requests

app = FastAPI()

# Load ResNet model and processor
print("Loading ResNet model...")
model = ResNetForImageClassification.from_pretrained("microsoft/resnet-50")
processor = AutoImageProcessor.from_pretrained("microsoft/resnet-50")
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = model.to(device)
print("Model loaded successfully!")

OBJECT_DETECTION_SERVICE_URL = "http://object-detection-service:8001/notify/"


def load_image(file: UploadFile):
    """
    Load an image from the uploaded file.
    """
    try:
        return Image.open(BytesIO(file.file.read())).convert("RGB")
    except Exception as e:
        raise ValueError(f"Failed to load image: {str(e)}")


def classify_image(image: Image):
    """
    Classify the image using ResNet.
    Returns the predicted label and confidence score.
    """
    # Prepare the input
    inputs = processor(image, return_tensors="pt").to(device)

    # Perform inference
    with torch.no_grad():
        logits = model(**inputs).logits

    # Get the predicted label
    predicted_label_idx = logits.argmax(-1).item()
    predicted_label = model.config.id2label[predicted_label_idx]
    confidence_score = torch.softmax(logits, dim=-1)[0, predicted_label_idx].item()

    return predicted_label, confidence_score


def notify_object_detection_service(message: dict):
    """
    Send a message to the Object Detection Service.
    """
    try:
        response = requests.post(OBJECT_DETECTION_SERVICE_URL, json=message)
        print(f"Notification sent. Response: {response.status_code}, {response.json()}")
    except Exception as e:
        print(f"Failed to notify Object Detection Service: {str(e)}")

### MAIN ENDPOINT ###

@app.post("/classify/")
async def classify(file: UploadFile = File(...)):
    """
    Endpoint to classify objects in an image and check for "ambulance".
    """
    try:
        # Load the image
        image = load_image(file)
        print("Image loaded successfully.")

        # Classify the image
        predicted_label, confidence_score = classify_image(image)
        print(f"Classification completed. Label: {predicted_label}, Confidence: {confidence_score:.3f}")

        # Check for "ambulance"
        if predicted_label.lower() == "ambulance":  # Ensure the label matches your use case
            print(f"Ambulance detected with confidence {confidence_score:.3f}.")

            # Notify the Object Detection Service
            notify_object_detection_service({
                "detected_object": "ambulance",
                "confidence": round(confidence_score, 3)
            })

            # Return a success response
            return {
                "status": "success",
                "message": "Ambulance detected and notification sent.",
                "confidence": round(confidence_score, 3),
            }

        # If no ambulance was detected
        return {"status": "success", "message": "No ambulance detected."}

    except ValueError as ve:
        return {"status": "error", "message": f"Image loading error: {str(ve)}"}
    except Exception as e:
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}

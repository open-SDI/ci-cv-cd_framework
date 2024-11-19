from fastapi import FastAPI, UploadFile, File
from diffusers import StableDiffusionUpscalePipeline
from PIL import Image
import torch
from io import BytesIO
from fastapi.responses import StreamingResponse
import requests

app = FastAPI()

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("Device selected: ", device)
print("Loading the upscaling model...")
model_id = "stabilityai/stable-diffusion-x4-upscaler"
pipeline = StableDiffusionUpscalePipeline.from_pretrained(
    model_id, torch_dtype=torch.float16
).to(device)
print("Model loaded successfully!")

IMAGE_CLASSIFICATION_URL = "http://image-classification-service:8003/classify/"


def load_image(file: UploadFile):
    """
    Load an image from the uploaded file.
    """
    try:
        return Image.open(BytesIO(file.file.read())).convert("RGB")
    except Exception as e:
        raise ValueError(f"Failed to load image: {str(e)}")


def upscale_image_with_pipeline(image: Image, prompt: str = "a generic vehicle"):
    """
    Upscale the given image using the Stable Diffusion Upscale Pipeline.
    """
    try:
        upscaled_image = pipeline(prompt=prompt, image=image).images[0]
        return upscaled_image
    except Exception as e:
        raise RuntimeError(f"Upscaling failed: {str(e)}")


def image_to_bytes(image: Image):
    """
    Convert an image to bytes for response.
    """
    img_byte_arr = BytesIO()
    image.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)
    return img_byte_arr


def forward_to_classification_service(upscaled_image: Image):
    """
    Forward the upscaled image to the Image Classification Service.
    """
    # Convert the image to bytes
    img_byte_arr = BytesIO()
    upscaled_image.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)

    # Send the image to the classification service
    try:
        response = requests.post(
            IMAGE_CLASSIFICATION_URL,
            files={"file": ("upscaled_image.png", img_byte_arr.getvalue())},
        ) 
        print(f"Classification Service Response: {response.status_code}, {response.json()}")
        return response.json()
    except Exception as e:
        raise RuntimeError(f"Failed to forward image to classification service: {str(e)}")


### MAIN ENDPOINT ###


@app.post("/upscale/")
async def upscale_image(file: UploadFile = File(...)):
    """
    Upscale a received image and forward it to the classification service.
    """
    try:
        # Load the low-resolution image
        low_res_image = load_image(file)
        print("Image loaded successfully.")

        # Perform upscaling
        prompt = "a generic vehicle"  # Modify as needed
        upscaled_image = upscale_image_with_pipeline(low_res_image, prompt)
        print("Image upscaled successfully.")

        # Forward upscaled image to classification service
        classification_response = forward_to_classification_service(upscaled_image)

        return {
            "status": "success",
            "message": "Image upscaled and forwarded to classification service.",
            "classification_response": classification_response,
        }

    except ValueError as ve:
        return {"status": "error", "message": f"Image loading error: {str(ve)}"}
    except RuntimeError as re:
        return {"status": "error", "message": f"Processing error: {str(re)}"}
    except Exception as e:
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}

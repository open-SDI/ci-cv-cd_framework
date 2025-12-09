from fastapi import FastAPI

app = FastAPI(title="CI Orchestration Engine", version="0.1.0")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app="main:app", host="0.0.0.0", port=8000, reload=True)

# app/main.py

from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.routers.hospitals import router as hospitals_router
from app.hospital_client import close_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: nothing to initialize yet (client is lazy-initialized)
    yield
    # Shutdown: cleanly close the shared httpx client
    # Without this, connections hang on process exit
    await close_client()


app = FastAPI(
    title="Hospital Bulk Processor",
    description="Bulk CSV upload system for hospital directory management.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(hospitals_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
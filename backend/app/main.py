"""FastAPI application entry point — P1 minimal (health check only)."""

from fastapi import FastAPI

from app.config import settings
from app.infra.logger import setup_logging

setup_logging()

app = FastAPI(title=settings.APP_NAME, docs_url="/docs", redoc_url="/redoc")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.APP_ENV}

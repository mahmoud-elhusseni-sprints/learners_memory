"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from learner_memory.api.v1 import ingest, learners
from learner_memory.core.config import get_settings
from learner_memory.core.logging import configure_logging, get_logger
from learner_memory.extractors.registry import load_extractors, supported_sources
from learner_memory.storage.supabase import get_storage
from learner_memory.vector.qdrant import get_card_index

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    load_extractors()                       # populates the extractor registry
    await get_card_index().ensure_collection()
    get_storage().ensure_bucket()
    log.info("api.started", env=settings.env, sources=supported_sources())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Learner Memory & Profile", version="0.1.0", lifespan=lifespan)
    app.include_router(learners.router, prefix="/v1")
    app.include_router(ingest.router, prefix="/v1")

    @app.get("/healthz", tags=["ops"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz():
        return {"status": "ready", "sources": supported_sources()}

    Instrumentator().instrument(app).expose(app, endpoint="/metrics", tags=["ops"])
    return app


app = create_app()

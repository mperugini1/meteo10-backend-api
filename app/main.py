"""Aplicação FastAPI do Meteo10."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.errors import install_error_handlers
from app.routers import admin, alarms, auth, health, ingest, stations
from app.tasks import PeriodicTasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

API_PREFIX = "/api/v1"

if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env, send_default_pii=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = PeriodicTasks() if settings.background_tasks else None
    if tasks:
        tasks.start()
    yield
    if tasks:
        tasks.stop()


def create_app() -> FastAPI:
    docs = not settings.is_production
    app = FastAPI(
        title="Meteo10 API",
        version="1.0.0",
        description="Backend da estação meteorológica Meteo10 (PCS3858 – Poli-USP).",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    install_error_handlers(app)
    for module in (health, auth, ingest, stations, alarms, admin):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()

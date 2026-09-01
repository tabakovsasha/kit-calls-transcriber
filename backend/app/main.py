"""FastAPI application entrypoint.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Assembly order matters:
1. logging is configured before anything can log a secret;
2. settings are read once (``get_settings`` is cached) and fail fast when a
   required secret is missing;
3. routers are mounted under a single ``/api`` prefix, matching the relative
   URLs already produced by ``media_service.build_audio_url``;
4. domain errors are translated to a stable JSON envelope by one handler
   instead of being re-raised as HTTPException in every route;
5. background workers live for exactly the lifespan of the application.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routers import (
    audio,
    auth,
    calls,
    connections,
    profile,
    queue,
    schedules,
    settings,
    users,
    ws,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.runtime import runtime
from app.db.session import SessionFactory, engine
from app.services import media_service, whisper_service, worker_service
from app.services.bootstrap_service import ensure_bootstrap_admin

logger = logging.getLogger(__name__)

API_PREFIX = "/api"

# Every router already declares its own prefix and tags, so no path is
# spelled twice here.
ROUTERS = (
    auth.router,
    profile.router,
    users.router,
    connections.router,
    calls.router,
    queue.router,
    schedules.router,
    audio.router,
    settings.router,
)


async def _verify_database() -> None:
    """Fail fast when the database is unreachable or migrations were not run."""
    async with SessionFactory() as db:
        await db.execute(text("SELECT 1"))
        await db.execute(text("SELECT 1 FROM users LIMIT 1"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Thread counts must be set before the first inference, and only once.
    runtime.configure_torch_threads()
    media_service.ensure_audio_dir()
    whisper_service.ensure_models_dir()

    await _verify_database()

    async with SessionFactory() as db:
        await ensure_bootstrap_admin(db, settings)
        # Load persisted settings (default model, profile) into the in-process
        # cache so the worker and enqueue logic never query per item.
        from app.services import app_settings_service
        await app_settings_service.load_all(db)
        # Apply the persisted profile and model so the runtime plan reflects what
        # the admin chose, not only the .env default.
        profile = await app_settings_service.resolve_profile(db)
        model = await app_settings_service.resolve_default_model(db)
        await runtime.apply_profile(profile, whisper_model=model)

    worker_service.start_workers()
    logger.info(
        "[APP] %s запущен (env=%s, profile=%s, concurrency=%s)",
        settings.app_name,
        settings.app_env,
        runtime.get("profile"),
        runtime.get("max_concurrency"),
    )

    try:
        yield
    finally:
        await worker_service.stop_workers()
        await engine.dispose()
        logger.info("[APP] Остановлен")


def _error_response(
    status_code: int, code: str, message: str, details: object = None
) -> JSONResponse:
    payload: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # Client faults are noise at ERROR level; upstream/server ones are not.
        log = logger.error if exc.status_code >= 500 else logger.info
        log(
            "[API] %s %s -> %s: %s",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
        )
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Only loc/msg are echoed: the raw error entries carry the rejected
        # input, which may be a password or an upstream token.
        return _error_response(
            422,
            "VALIDATION_FAILED",
            "Некорректные параметры запроса",
            [
                {"loc": [str(part) for part in item.get("loc", ())], "msg": item.get("msg", "")}
                for item in exc.errors()
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _error_response(
            exc.status_code, f"HTTP_{exc.status_code}", str(exc.detail)
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak an internal message or traceback to the client.
        logger.error(
            "[API] Необработанная ошибка %s %s",
            request.method,
            request.url.path,
            exc_info=True,
        )
        return _error_response(500, "INTERNAL_ERROR", "Внутренняя ошибка сервера")


def create_app() -> FastAPI:
    configure_logging(extra_loggers=("uvicorn", "uvicorn.access", "uvicorn.error"))
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="2.0.0",
        lifespan=lifespan,
        # Schema browsing is a development convenience, not a production one.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Origins come from CORS_ORIGINS; credentials are enabled because the
    # refresh token travels in a cookie, so a wildcard is not usable here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    register_exception_handlers(app)

    for router in ROUTERS:
        app.include_router(router, prefix=API_PREFIX)

    # The websocket lives outside the /api prefix: it is a separate transport,
    # not a REST resource, and it authenticates with a ticket rather than a
    # bearer header.
    app.include_router(ws.router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Liveness probe: no auth, no database, no secrets."""
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()

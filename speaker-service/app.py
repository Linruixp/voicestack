"""FastAPI application for the localhost speaker-service.

``create_app`` is the composition root: it wires the injected seams (registry
factory, meeting runner, identity embedder, audio loader, token reader) into
``app.state`` and mounts the route modules. The module-level ``app`` is what
``uvicorn app:app`` serves on ``127.0.0.1:3910``.

Launch policy: bind loopback only (``VASTACK_BIND``, default ``127.0.0.1``),
no CORS middleware, and API docs disabled - the only browser client is the
same-origin UI, which authenticates with a server-minted session cookie.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import config
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

import api_routes
import api_speaker_routes
import ui
from api_auth import SessionStore, allowed_origins
from api_service import (
    AudioLoader,
    Embedder,
    LazyEmbedder,
    MeetingRunner,
    ServiceDeps,
    default_embedder_factory,
    default_runner,
    load_audio,
)
from config import Settings, get_settings
from idle import ActivityTracker, IdleWatchdog, terminate_process
from registry import Registry, open_registry


def _jobs_active(registry_factory: Callable[[], Registry]) -> bool:
    """Read the durable jobs table for a queued/running job (watchdog check)."""
    with registry_factory() as registry:
        return registry.has_active_jobs()


def create_app(
    *,
    settings: Settings | None = None,
    registry_factory: Callable[[], Registry] = open_registry,
    runner: MeetingRunner = default_runner,
    embedder_factory: Callable[[], Embedder] = default_embedder_factory,
    audio_loader: AudioLoader = load_audio,
    read_token: Callable[[], str | None] | None = None,
    enable_idle_watchdog: bool = True,
) -> FastAPI:
    resolved = settings if settings is not None else get_settings()
    activity = ActivityTracker()
    watchdog = (
        IdleWatchdog(
            tracker=activity,
            jobs_active=lambda: _jobs_active(registry_factory),
            idle_after_s=float(resolved.idle_exit_s),
            on_idle=terminate_process,
        )
        if enable_idle_watchdog and resolved.idle_exit_s > 0
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(watchdog.run()) if watchdog is not None else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="speaker-service",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.activity = activity
    app.state.idle_watchdog = watchdog
    app.state.registry_factory = registry_factory
    app.state.read_token = (
        read_token if read_token is not None else config.read_service_token
    )
    app.state.sessions = SessionStore()
    app.state.allowed_origins = allowed_origins(resolved.port)
    app.state.deps = ServiceDeps(
        runner=runner,
        embedder=LazyEmbedder(embedder_factory),
        audio_loader=audio_loader,
        settings=resolved,
    )
    app.include_router(ui.router)
    app.include_router(api_routes.router)
    app.include_router(api_speaker_routes.router)
    app.mount("/static", StaticFiles(directory=ui.STATIC_DIR), name="static")

    @app.middleware("http")
    async def track_activity(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Count in-flight requests; refresh the keepalive on ``/health``."""
        activity.request_started()
        try:
            return await call_next(request)
        finally:
            activity.request_finished(request.url.path)

    return app


app = create_app()

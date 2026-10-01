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

from collections.abc import Callable

import config
from fastapi import FastAPI
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
from registry import Registry, open_registry


def create_app(
    *,
    settings: Settings | None = None,
    registry_factory: Callable[[], Registry] = open_registry,
    runner: MeetingRunner = default_runner,
    embedder_factory: Callable[[], Embedder] = default_embedder_factory,
    audio_loader: AudioLoader = load_audio,
    read_token: Callable[[], str | None] | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else get_settings()
    app = FastAPI(
        title="speaker-service",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
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
    return app


app = create_app()

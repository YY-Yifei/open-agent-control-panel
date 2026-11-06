"""Application factory: wiring, static UI, error mapping and the probe loop."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, api, adp
from .adp.probe import ProbeLoop
from .adp.routes import router as adp_router
from .adp.service import RegistryError, RegistryService
from .a2a.client import A2AClient
from .config import Settings
from .db import Database
from .tasks import TaskService

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    The panel owns four stateful things: the database, the registry service, the
    A2A client and the task ledger. They are constructed here and hung off
    ``app.state`` so tests (and embedders) can reach them without globals.
    """
    settings = settings or Settings.from_env()

    database = Database(settings.db_path)
    database.init()
    registry = RegistryService(database, settings)
    a2a_client = A2AClient(
        allow_private=settings.allow_private_agents,
        timeout=settings.a2a_timeout,
        user_agent=settings.user_agent,
    )
    tasks = TaskService(database, registry, a2a_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loop: ProbeLoop | None = None
        if settings.probe_enabled:
            loop = ProbeLoop(
                registry,
                concurrency=settings.probe_concurrency,
            )
            loop.start()
        app.state.probe_loop = loop
        try:
            yield
        finally:
            if loop is not None:
                loop.stop()

    app = FastAPI(
        title=settings.display_name,
        version=__version__,
        description=(
            "A registry and control plane for A2A agents. "
            "Implements the A2A Discover Protocol (ADP) Core operations."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.state.settings = settings
    app.state.db = database
    app.state.registry = registry
    app.state.tasks = tasks
    app.state.a2a = a2a_client

    app.include_router(adp_router)
    app.include_router(api.router, dependencies=[Depends(api.require_panel_token)])

    @app.exception_handler(RegistryError)
    async def _registry_error(_request: Request, exc: RegistryError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok", "version": __version__}

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/favicon.svg", include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    log.info("panel ready: %s (api base %s)", settings.display_name, settings.api_base)
    return app

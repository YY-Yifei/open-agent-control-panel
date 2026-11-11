"""Application factory: wiring, static UI, error mapping and the probe loop."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, api, adp
from .adp.fetcher import GuardedFetcher
from .adp.probe import ProbeLoop
from .adp.routes import router as adp_router
from .adp.service import RegistryError, RegistryService
from .a2a.client import A2AClient
from .config import Settings
from .db import Database
from .errors import PanelError, normalize_lang
from .push import router as push_router
from .sessions import SessionService
from .tasks import TaskService

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

#: Cap on a panel JSON request body. Generous for the supported shape (four 5 MB
#: images are ~27 MB once base64-encoded) and small enough that parsing it is not
#: a hazard. The panel token protects the endpoint, and it is unset by default.
PANEL_BODY_LIMIT = 64 * 1024 * 1024

#: Cap on an ADP request body, which is always a few hundred bytes
#: (`{"url": ...}` on an unauthenticated route). Sharing the panel's
#: image-derived cap would let anybody make the registry parse tens of MB.
ADP_BODY_LIMIT = 1024 * 1024


class _BodyTooLarge(BaseException):
    """Raised from the wrapped receive once the body passes the cap.

    Deliberately a ``BaseException``: FastAPI wraps body parsing in
    ``except Exception`` and re-raises a generic 400 ("There was an error parsing
    the body"), which would hide the panel's own 413 from the client entirely —
    the cap would still work, but a caller could never tell why.
    """


def _body_limit(settings: Settings) -> int:
    """The request cap, never tighter than the deployment's own image budget.

    A deployment that raises ``max_image_bytes`` / ``max_images_per_message`` must
    not end up with a cap that rejects a message the service would accept.
    """
    wanted = int(settings.max_image_bytes * 4 / 3) * max(1, settings.max_images_per_message)
    return max(PANEL_BODY_LIMIT, wanted + (1 << 20))


async def _too_large(scope: Any, receive: Any, send: Any, limit: int) -> None:
    """Answer an oversized body in the contract of the surface it arrived on.

    `/api/panel` gets the panel's error shape and the caller's language, like
    every other panel error (`detail` + `code`), so the UI can show it instead
    of falling back to its generic message. `/v1` gets the ADP shape in English
    (`code` + `message`, exactly what `RegistryError.body()` renders): a
    localized panel body on the published contract is the kind of leak the
    "ADP keeps its own shape" comment above is there to prevent.
    """
    if str(scope.get("path", "")).startswith("/v1/"):
        # The spec names no code for an oversized body, so this follows the
        # upper-snake spelling of the ones it does name (BAD_URL, URL_TAKEN).
        response = JSONResponse(
            {"code": "PAYLOAD_TOO_LARGE", "message": f"request body exceeds {limit} bytes"},
            status_code=413,
        )
        await response(scope, receive, send)
        return
    headers = {key.lower(): value for key, value in scope.get("headers") or []}
    lang = normalize_lang(headers.get(b"accept-language", b"").decode("latin-1"))
    error = PanelError("payload_too_large", status=413, max=f"{limit / (1024 * 1024):.0f}")
    response = JSONResponse(error.payload(lang), status_code=error.status)
    await response(scope, receive, send)


class _BodyLimit:
    """Refuse an oversized panel body *before* FastAPI parses it.

    Without this, per-image limits do not bound memory: the body is parsed into
    Python strings first and only then checked, so `max_images_per_message *
    max_image_bytes` worth of base64 (or any lie about it) is materialized. The
    declared Content-Length is checked first, and the running total is counted as
    well, because the header is optional and a sender may understate it.
    """

    def __init__(self, app: Any, limit: int) -> None:
        self.app = app
        self.limit = int(limit)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # The panel API and the public ADP registration route both take a JSON
        # body. Push keeps its own, tighter cap inside its handler.
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not path.startswith(("/api/panel", "/v1/")):
            await self.app(scope, receive, send)
            return

        limit = ADP_BODY_LIMIT if path.startswith("/v1/") else self.limit

        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _too_large(scope, receive, send, limit)
            return

        seen = 0

        async def _limited() -> Any:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, _limited, send)
        except _BodyTooLarge:
            await _too_large(scope, receive, send, limit)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    The panel owns five stateful things: the database, the registry service, the
    A2A client, the task ledger and the conversations built on top of it. They are
    constructed here and hung off ``app.state`` so tests (and embedders) can reach
    them without globals.
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
    tasks = TaskService(database, registry, a2a_client, settings)
    # A separate fetcher from the registry's, with two differences that matter:
    # it fetches bytes rather than JSON, and it is capped by the *inbound image*
    # limit. It keeps every guard the card fetcher has — public addresses only,
    # DNS pinned after validation, redirects re-checked hop by hop, body capped
    # while streaming — because a URL named by an agent is a URL named by a
    # stranger, registration or not.
    media_fetcher: GuardedFetcher | None = None
    if settings.fetch_inbound_files:
        media_fetcher = GuardedFetcher(
            max_bytes=settings.max_inbound_image_bytes,
            connect_timeout=min(5.0, settings.a2a_timeout),
            total_timeout=min(10.0, settings.a2a_timeout),
            user_agent=settings.user_agent,
            allow_private=settings.allow_private_agents,
            # Bytes, not JSON: a server that content-negotiates must not answer 406.
            accept="*/*",
        )
    sessions = SessionService(database, registry, tasks, settings, fetcher=media_fetcher)

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
    app.state.sessions = sessions
    app.state.a2a = a2a_client
    app.state.media_fetcher = media_fetcher
    app.add_middleware(_BodyLimit, limit=_body_limit(settings))

    app.include_router(adp_router)
    # Agents call this one, so it carries its own per-task token instead of the
    # panel token, and it is mounted before the protected surface on purpose.
    app.include_router(push_router)
    app.include_router(api.router, dependencies=[Depends(api.require_panel_token)])

    @app.exception_handler(RegistryError)
    async def _registry_error(_request: Request, exc: RegistryError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(PanelError)
    async def _panel_error(request: Request, exc: PanelError) -> JSONResponse:
        """Render a panel error in the caller's language (Chinese by default).

        The ADP surface above keeps its contract shape and English messages; the
        panel's own endpoints answer in the language the UI is showing, which is
        what ``Accept-Language`` carries. The stable ``code`` rides along so
        nothing has to match on prose.
        """
        lang = normalize_lang(request.headers.get("accept-language"))
        return JSONResponse(exc.payload(lang), status_code=exc.status, headers=exc.headers)

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

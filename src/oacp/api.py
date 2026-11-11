"""Panel API — everything the web UI talks to.

These endpoints are *not* part of the ADP contract: they exist so a human can
see registered agents, submit A2A tasks and follow them. Keeping them under
``/api/panel`` is what lets ``/.well-known`` and ``/v1`` stay exactly the
protocol.

Errors raised here (``PanelError``) carry a stable code and a status; the
handler in ``app.py`` renders them in the language the caller asked for
(``Accept-Language``, Chinese by default). Nothing in this module spells out a
human-readable message — see ``oacp.errors`` for the catalog.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, WebSocket
from pydantic import BaseModel, Field

from . import __version__
from .adp import card as cardmod
from .adp.service import RegistryError
from .errors import PanelError
from .sessions import SessionService
from .tasks import TaskService
from .util import json_loads

router = APIRouter(prefix="/api/panel", tags=["panel"])


# --------------------------------------------------------------------- security


def require_panel_token(
    request: Request,
    x_panel_token: str | None = Header(default=None, alias="X-Panel-Token"),
    authorization: str | None = Header(default=None),
) -> None:
    """Reject callers without the panel token, when one is configured.

    Also records the outcome on the request: ``request.state.panel_token_ok`` is
    True only when a token was configured *and* matched. Endpoints that grant
    operator-level authority (removal without a manage token) read that flag, so
    the credential is never inferred twice in two slightly different ways.
    """
    request.state.panel_token_ok = False
    expected = request.app.state.settings.panel_token
    if not expected:
        return
    candidate = x_panel_token or ""
    if not candidate and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise PanelError(
            "panel_token_required",
            status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.panel_token_ok = True


def operator_authority(request: Request) -> bool:
    """True when the caller may act on the registry *as its operator*.

    Two ways in, and only two:

    * they proved the panel token — that is the operator credential, so a
      deployment exposed to the network can hand out exactly one;
    * the panel is a loopback-only deployment *and* no panel token is set. There
      the UI is already reachable only from this machine and unauthenticated, so
      "whoever can open the panel" is the operator by construction.

    A network-exposed panel with no token gets **no** such authority: removal
    then has to present the manage token from first registration. Guessing
    otherwise would turn one open panel into a way to delete other people's
    entries.
    """
    if getattr(request.state, "panel_token_ok", False):
        return True
    settings = request.app.state.settings
    return settings.local_mode and not settings.panel_token


def removal_policy(request: Request) -> dict[str, Any]:
    """What the UI needs to know about removing agents, in one place."""
    settings = request.app.state.settings
    if settings.panel_token:
        return {"operator": True, "reason": "panel-token"}
    if settings.local_mode:
        return {"operator": True, "reason": "local-mode"}
    return {"operator": False, "reason": "manage-token"}


# ----------------------------------------------------------------------- schemas


class SubmitTask(BaseModel):
    """A first user message addressed to an agent."""

    text: str = Field(min_length=1, max_length=20000)
    contextId: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None


class FollowUp(BaseModel):
    """A further message inside an existing task/context."""

    text: str = Field(min_length=1, max_length=20000)
    metadata: dict[str, Any] | None = None


#: Backstop only: the real limit is computed by ``media.prepare`` from the
#: deployment's ``max_image_bytes``. This exists so the server never parses a
#: body of absurd size into a Python string; 48 MB of base64 is ~36 MB of bytes.
#: The *request-level* cap (the one that bounds the product of this and
#: ``MAX_ATTACHMENTS`` before parsing) is ``app.PANEL_BODY_LIMIT``.
MAX_ATTACHMENT_B64 = 48 * 1024 * 1024
#: How many attachments one request may carry. The service enforces the real cap;
#: this only stops "tens of thousands of empty objects" from being parsed at all.
MAX_ATTACHMENTS = 32


class Attachment(BaseModel):
    """One image the user attached, already base64-encoded.

    The bytes arrive in the JSON body instead of through a multipart upload: the
    send is then a single request, so there is no half-finished upload to reap and
    no second endpoint whose only job would be to hold bytes that are about to be
    forwarded anyway.
    """

    mime: str = Field(default="", max_length=100)
    name: str = Field(default="", max_length=200)
    data: str = Field(min_length=1, max_length=MAX_ATTACHMENT_B64)


class NewSession(BaseModel):
    """A conversation to open, optionally with the message that starts it."""

    text: str | None = Field(default=None, max_length=20000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    title: str | None = Field(default=None, max_length=80)


class SendMessage(BaseModel):
    """One line the user says to an agent, optionally with pictures."""

    text: str = Field(default="", max_length=20000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class RenameSession(BaseModel):
    """A new title for a conversation."""

    title: str = Field(min_length=1, max_length=80)


class DeleteAgent(BaseModel):
    """The manage token returned when the agent was first registered.

    Optional on the wire (see ``delete_agent``): an operator may remove an entry
    without one. An empty string is treated as "no token given" rather than as a
    validation error, so the UI can always send the body shape it has.
    """

    token: str = Field(default="", max_length=500)


# ----------------------------------------------------------------------- helpers


def _tasks(request: Request) -> TaskService:
    return request.app.state.tasks


def _registry(request: Request):
    return request.app.state.registry


def _sessions(request: Request) -> SessionService:
    return request.app.state.sessions


def _client_ip(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def _header_name(name: Any) -> str:
    """A filename safe inside an HTTP header value (those are not UTF-8 safe)."""
    text = "".join(
        char if 32 <= ord(char) < 127 and char not in '"\\;' else "_" for char in str(name or "")
    ).strip()
    return text or "image"


def _panel_error(exc: RegistryError, *, agent_id: str) -> PanelError:
    """Translate a registry error into a localized panel error.

    ``DELETE /api/panel/...`` is the panel's own surface, so it answers in the
    UI's language; the registry speaks the contract's English. Without this
    mapping a wrong manage token would surface as ``{"code": "UNAUTHORIZED"}``
    in English, which is exactly the leak the panel error catalog exists to stop.
    """
    if exc.status == 404:
        return PanelError("agent_not_found", status=404, agent_id=agent_id)
    if exc.code == "UNAUTHORIZED":
        return PanelError("manage_token_mismatch", status=401, agent_id=agent_id)
    # Nothing else is reachable through deletion today; keep the status and code
    # rather than inventing a message if that ever changes.
    return PanelError(exc.code.lower(), status=exc.status, agent_id=agent_id)


def _agent_totals(request: Request) -> dict[str, int]:
    with request.app.state.db.connect() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS n FROM agents GROUP BY status"
        ).fetchall()
    by_status = {row["status"]: int(row["n"]) for row in rows}
    return {
        "total": sum(by_status.values()),
        "ok": by_status.get("ok", 0),
        "down": by_status.get("down", 0),
    }


def _with_counts(request: Request, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agent_ids = [item["id"] for item in items]
    counts = _tasks(request).agent_counts(agent_ids)
    fallback = {"taskCount": 0, "activeTaskCount": 0, "lastTaskAt": None}
    sessions = _sessions(request).agent_counts(agent_ids)
    session_fallback: dict[str, Any] = {"sessionCount": 0, "lastSessionAt": None}
    images = _image_inputs(request, agent_ids)
    for item in items:
        item.update(counts.get(item["id"], fallback))
        item.update(sessions.get(item["id"], session_fallback))
        item["acceptsImages"] = images.get(item["id"])
    return items


def _image_inputs(request: Request, agent_ids: list[str]) -> dict[str, bool | None]:
    """Whether each agent takes images: True, False, or None when it never said.

    Read from the stored card here rather than added to the ADP record, because
    that object is closed (``additionalProperties: false``) — a key the contract
    does not define would make the response non-conformant for every consumer, for
    the convenience of one panel.
    """
    if not agent_ids:
        return {}
    placeholders = ",".join("?" for _ in agent_ids)
    with request.app.state.db.connect() as connection:
        rows = connection.execute(
            f"SELECT id, card FROM agents WHERE id IN ({placeholders})",
            list(agent_ids),
        ).fetchall()
    return {row["id"]: cardmod.accepts_images(json_loads(row["card"], {})) for row in rows}


# ---------------------------------------------------------------------- summary


@router.get("/summary")
def summary(request: Request) -> dict[str, Any]:
    """Counts and deployment facts for the dashboard header."""
    settings = request.app.state.settings
    return {
        "version": __version__,
        "name": settings.display_name,
        "publicUrl": settings.public_url,
        "apiBase": settings.api_base,
        "registryCard": settings.registry_card(),
        "agents": _agent_totals(request),
        "tasks": _tasks(request).stats(),
        "sessions": _sessions(request).stats(),
        "probe": {"enabled": settings.probe_enabled, "interval": settings.probe_interval},
        # The UI gets the same limits the server enforces, so it can refuse a
        # 20 MB photo before base64-ing it into a request that would be rejected.
        "images": {
            "maxBytes": settings.max_image_bytes,
            "maxPerMessage": settings.max_images_per_message,
        },
        "panelTokenRequired": bool(settings.panel_token),
        "pushEnabled": settings.push_enabled,
        "allowPrivateAgents": settings.allow_private_agents,
        "localMode": settings.local_mode,
        "cardValidation": settings.card_validation,
        "removal": removal_policy(request),
    }


# ----------------------------------------------------------------------- agents


@router.get("/agents")
def list_agents(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, Any]:
    """Registered agents, enriched with local task counts."""
    result = _registry(request).list_agents(
        q=q, status=status, full=False, limit=limit, cursor=cursor
    )
    result["items"] = _with_counts(request, result["items"])
    return result


@router.get("/agents/{agent_id}")
def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """One agent: full record, derived interfaces and its recent tasks."""
    record = _registry(request).get(agent_id)
    if record is None:
        raise PanelError("agent_not_found", status=404, agent_id=agent_id)

    card = record.get("card") if isinstance(record.get("card"), dict) else {}
    # The record itself is the contract's AgentRecord, which carries no facets.
    # This endpoint is the panel's own, and the UI shows a name and skills, so
    # the facets are added here rather than smuggled into the contract shape.
    record = {**record, **cardmod.card_summary(card)}
    counts = _tasks(request).agent_counts([agent_id]).get(
        agent_id, {"taskCount": 0, "activeTaskCount": 0, "lastTaskAt": None}
    )
    recent = _tasks(request).list_tasks(agent_id=agent_id, limit=20)
    return {
        "agent": record,
        "interfaces": cardmod.interfaces(card),
        "endpoint": cardmod.pick_endpoint(card),
        "stats": counts,
        "tasks": recent["items"],
        "removal": removal_policy(request),
    }


@router.post("/agents/{agent_id}/refresh")
def refresh_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """Re-fetch the card right now instead of waiting for the probe loop."""
    record = _registry(request).probe(agent_id)
    if record is None:
        raise PanelError("agent_not_found", status=404, agent_id=agent_id)
    return {"agent": record}


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(
    request: Request,
    agent_id: str,
    payload: DeleteAgent | None = Body(default=None),
) -> Response:
    """Unregister an agent.

    Two ways to be allowed, and the order matters: if a manage token is sent, it
    is verified exactly as the contract requires — ownership wins over office. If
    no token is sent, the caller must be the panel's operator
    (``operator_authority``) and the entry is removed on that authority, with the
    removal written to the registration log.

    The tasks the agent already ran stay in the ledger: task rows carry their own
    copy of the agent's name and URL, so the history remains readable and honest
    after the entry is gone.
    """
    registry = _registry(request)
    token = (payload.token if payload else "").strip()

    if token:
        try:
            registry.delete(agent_id, token)
        except RegistryError as exc:
            raise _panel_error(exc, agent_id=agent_id) from exc
        return Response(status_code=204)

    if not operator_authority(request):
        raise PanelError("manage_token_required", status=401, agent_id=agent_id)

    try:
        registry.force_delete(agent_id, client_ip=_client_ip(request))
    except RegistryError as exc:
        raise _panel_error(exc, agent_id=agent_id) from exc
    return Response(status_code=204)


# ------------------------------------------------------------------------ tasks


@router.post("/agents/{agent_id}/tasks", status_code=201)
def submit_task(request: Request, agent_id: str, payload: SubmitTask) -> dict[str, Any]:
    """Send a message to the agent and record the A2A task it creates."""
    task = _tasks(request).submit(
        agent_id,
        payload.text,
        context_id=payload.contextId,
        metadata=payload.metadata,
    )
    return {"task": task}


@router.get("/tasks")
def list_tasks(
    request: Request,
    agentId: str | None = None,
    state: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> dict[str, Any]:
    """Recent tasks, newest first."""
    return _tasks(request).list_tasks(
        agent_id=agentId, state=state, q=q, limit=limit, cursor=cursor
    )


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str) -> dict[str, Any]:
    """One task with its event log and raw payloads."""
    return {"task": _tasks(request).get(task_id)}


@router.post("/tasks/{task_id}/refresh")
def refresh_task(request: Request, task_id: str) -> dict[str, Any]:
    """Ask the agent for the current task state (``GetTask``)."""
    return {"task": _tasks(request).refresh(task_id)}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(request: Request, task_id: str) -> dict[str, Any]:
    """Ask the agent to cancel the task (``CancelTask``)."""
    return {"task": _tasks(request).cancel(task_id)}


@router.post("/tasks/{task_id}/messages", status_code=201)
def follow_up(request: Request, task_id: str, payload: FollowUp) -> dict[str, Any]:
    """Send another user message into the same task/context."""
    task = _tasks(request).follow_up(task_id, payload.text, metadata=payload.metadata)
    return {"task": task}


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(request: Request, task_id: str) -> Response:
    """Forget a task locally; the agent keeps its own copy."""
    _tasks(request).delete(task_id)
    return Response(status_code=204)


# --------------------------------------------------------------------- sessions
#
# The panel's surface is a conversation, not a task. These endpoints are the
# whole vocabulary the UI is allowed to know: agents, conversations, messages.
# A2A's own words (task, contextId, SendMessage, push notification) are an
# implementation detail of ``oacp.sessions`` and never appear here.


@router.get("/sessions")
def list_sessions(
    request: Request,
    agentId: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """Conversations, newest activity first; filter by agent when asked."""
    return _sessions(request).list_sessions(agent_id=agentId, limit=limit)


@router.post("/agents/{agent_id}/sessions", status_code=201)
def create_session(
    request: Request,
    agent_id: str,
    payload: NewSession | None = Body(default=None),
) -> dict[str, Any]:
    """Open a conversation with an agent, optionally sending its first message.

    Creating and sending in one call is the normal path, so a user who opens a
    conversation and then walks away leaves nothing behind.
    """
    session = _sessions(request).create(
        agent_id,
        text=payload.text if payload else None,
        attachments=[item.model_dump() for item in payload.attachments] if payload else None,
        title=payload.title if payload else None,
    )
    return {"session": session}


@router.get("/attachments/{attachment_id}")
def get_attachment(request: Request, attachment_id: int) -> Response:
    """Serve one image this panel stored, whichever direction it arrived from.

    The bytes are the panel's own copy of a picture — one a person attached, or one
    an agent sent back. The type is the one sniffed when the bytes were accepted,
    and the answer carries ``nosniff``: a stored blob must never be able to talk
    the browser into treating it as anything but the picture it was checked to be.

    A row whose ``skipped`` is set has no bytes — it exists so the conversation can
    say *why* a file is not on screen. Serving it would mean answering with an
    empty body and an image content type, so it is a 404 like any other missing
    attachment.
    """
    with request.app.state.db.connect() as connection:
        row = connection.execute(
            "SELECT mime, name, data, skipped FROM session_attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
    if row is None or row["skipped"]:
        raise PanelError("attachment_not_found", status=404)
    return Response(
        content=bytes(row["data"]),
        media_type=row["mime"],
        headers={
            "content-disposition": f'inline; filename="{_header_name(row["name"])}"',
            "x-content-type-options": "nosniff",
            # A row is immutable: this id always means these bytes.
            "cache-control": "private, max-age=31536000, immutable",
        },
    )


@router.get("/sessions/{session_id}")
def get_session(request: Request, session_id: str) -> dict[str, Any]:
    """One conversation with every message it holds."""
    return {"session": _sessions(request).get(session_id)}


@router.post("/sessions/{session_id}/messages", status_code=201)
def send_message(request: Request, session_id: str, payload: SendMessage) -> dict[str, Any]:
    """Say something to the agent and return the conversation as it now stands.

    The answer may be ``working``: the agent accepted the message and will report
    back later. The UI then polls ``/sync`` (or waits for a push callback).
    """
    return {
        "session": _sessions(request).send(
            session_id,
            payload.text,
            attachments=[item.model_dump() for item in payload.attachments],
        )
    }


@router.post("/sessions/{session_id}/sync")
def sync_session(request: Request, session_id: str) -> dict[str, Any]:
    """Ask the agent for the current turn's state, right now."""
    return {"session": _sessions(request).sync(session_id)}


@router.post("/sessions/{session_id}/cancel")
def cancel_session(request: Request, session_id: str) -> dict[str, Any]:
    """Stop the running turn."""
    return {"session": _sessions(request).cancel(session_id)}


@router.patch("/sessions/{session_id}")
def rename_session(request: Request, session_id: str, payload: RenameSession) -> dict[str, Any]:
    """Rename a conversation."""
    return {"session": _sessions(request).rename(session_id, payload.title)}


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(request: Request, session_id: str) -> Response:
    """Forget a conversation; the agent keeps its own copy of what was said."""
    _sessions(request).delete(session_id)
    return Response(status_code=204)


# ------------------------------------------------------------------------ agent
#
# The built-in assistant (docs/agent-native.md). The **browser** is the WebSocket
# caller of an ``aaas`` server: these endpoints only answer where to connect and
# what the panel holds. The capability list is built in the page
# (``web/agent/catalog.js``) because nine of those abilities — theme, language,
# system notification, draft, download — exist nowhere else.
#
# ``agent_policy`` is therefore applied where the declarations are built, not
# here. That is honest rather than lax: the page already holds the panel token,
# so it is the same authority as this process. The policy limits what a *model*
# is offered, and the confirm gate is what stops a mistaken call from landing.

#: Where the page connects when the aaas server expects a bearer token. The token
#: stays in this process; the browser only ever sees this same-origin path.
PROXY_PATH = "/api/panel/agent/ws"

#: Wire subprotocol of that socket (``aaas.SUBPROTOCOL``). Spelled out because the
#: panel does not depend on the SDK just to serve its own pages.
SUBPROTOCOL = "aaas.v1"

#: How many rows one search scope scans before the needle is judged against the
#: parsed record. Bounded so a one-letter query cannot walk the whole ledger.
SEARCH_SCAN = 200


@router.get("/agent")
def agent_config(request: Request) -> dict[str, Any]:
    """Where the drawer should connect, resolved here so the page never guesses.

    ``wsUrl`` is the whole answer: the aaas URL when the server takes no token,
    otherwise this proxy's path. The browser has exactly one source of truth, and
    an unconfigured or switched-off deployment says ``enabled: false`` — the page
    then shows no entry at all rather than a button that cannot work.
    """
    settings = request.app.state.settings
    if not settings.agent_enabled:
        return {
            "enabled": False,
            "wsUrl": None,
            "viaProxy": False,
            "policy": settings.agent_policy,
            "modelHint": "",
        }
    if settings.aaas_token:
        # A same-origin *path*, resolved by the browser against its own origin.
        # Building an absolute URL here would mean trusting `Host` (client-supplied)
        # or `request.url.scheme` (wrong behind a TLS-terminating proxy) — and the
        # page appends its panel token to whatever we return, so a rewritten Host
        # would hand that credential to another authority.
        return {
            "enabled": True,
            "wsUrl": PROXY_PATH,
            "viaProxy": True,
            "policy": settings.agent_policy,
            "modelHint": settings.agent_model_hint,
        }
    return {
        "enabled": True,
        "wsUrl": settings.aaas_url,
        "viaProxy": False,
        "policy": settings.agent_policy,
        "modelHint": settings.agent_model_hint,
    }


def _like_pattern(needle: str) -> str:
    """A LIKE pattern matching the needle literally.

    ``%`` and ``_`` are wildcards, so an unescaped search for ``a_b`` would match
    ``axb`` and a lone ``%`` would match the entire ledger — a wrong answer that
    looks like a working one.
    """
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _haystack_matches(needle: str, *fields: Any) -> bool:
    """Case-insensitive substring test over the given fields."""
    lowered = needle.lower()
    return any(lowered in str(field or "").lower() for field in fields)


@router.get("/search")
def search(
    request: Request,
    q: str = Query(min_length=1, max_length=100),
    scope: str = Query(default="all", pattern="^(all|agents|sessions|tasks)$"),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    """Find agents, conversations and tasks by any word they contain.

    This is the one read the page could not do itself: the browser holds a single
    agent's conversations at a time, so "which conversation mentioned this" needs
    the ledger. Read-only, three bounded scans, and the agent scope re-checks the
    parsed card — matching the raw JSON would also hit every key name (``name``,
    ``skills``) and hand back the whole registry for a query like "name".
    """
    needle = q.strip()
    if not needle:
        # ``q=%20`` passes ``min_length=1`` before the strip, and an empty needle
        # becomes ``%%`` — a wildcard that matches every row in all three scopes.
        return {
            "q": "",
            "scope": scope,
            "counts": {"agents": 0, "sessions": 0, "tasks": 0},
            "agents": [],
            "sessions": [],
            "tasks": [],
        }
    pattern = _like_pattern(needle)
    database = request.app.state.db

    agent_items: list[dict[str, Any]] = []
    session_items: list[dict[str, Any]] = []
    task_items: list[dict[str, Any]] = []

    with database.connect() as connection:
        if scope in ("all", "agents"):
            rows = connection.execute(
                "SELECT id, url, card, status FROM agents"
                " WHERE id LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\'"
                " OR card LIKE ? ESCAPE '\\'"
                " ORDER BY first_seen_at DESC LIMIT ?",
                (pattern, pattern, pattern, SEARCH_SCAN),
            ).fetchall()
            for row in rows:
                card = json_loads(row["card"], {})
                name = str(card.get("name") or "")
                description = str(card.get("description") or "")
                skills = [
                    str(skill.get("name") or "")
                    for skill in (card.get("skills") or [])
                    if isinstance(skill, dict)
                ]
                if not _haystack_matches(needle, row["id"], row["url"], name, description, *skills):
                    continue
                agent_items.append(
                    {
                        "id": row["id"],
                        "name": name or row["id"],
                        "url": row["url"],
                        "status": row["status"],
                    }
                )

        if scope in ("all", "sessions"):
            rows = connection.execute(
                "SELECT id, agent_id, agent_name, title, state, updated_at, message_count"
                " FROM sessions WHERE id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
                " OR agent_name LIKE ? ESCAPE '\\'"
                " ORDER BY updated_at DESC LIMIT ?",
                (pattern, pattern, pattern, SEARCH_SCAN),
            ).fetchall()
            session_items = [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "agentId": row["agent_id"],
                    "agentName": row["agent_name"],
                    "state": row["state"],
                    "updatedAt": row["updated_at"],
                    "messageCount": row["message_count"],
                }
                for row in rows
            ]

        if scope in ("all", "tasks"):
            rows = connection.execute(
                "SELECT id, agent_id, agent_name, title, state, created_at, session_id"
                " FROM tasks WHERE id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
                " OR input_text LIKE ? ESCAPE '\\' OR agent_name LIKE ? ESCAPE '\\'"
                " ORDER BY created_at DESC LIMIT ?",
                (pattern, pattern, pattern, pattern, SEARCH_SCAN),
            ).fetchall()
            task_items = [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "agentId": row["agent_id"],
                    "agentName": row["agent_name"],
                    "state": row["state"],
                    "createdAt": row["created_at"],
                    "sessionId": row["session_id"],
                }
                for row in rows
            ]

    return {
        "q": needle,
        "scope": scope,
        "counts": {
            "agents": len(agent_items),
            "sessions": len(session_items),
            "tasks": len(task_items),
        },
        "agents": agent_items[:limit],
        "sessions": session_items[:limit],
        "tasks": task_items[:limit],
    }


# ---------------------------------------------------------------- agent: socket
#
# Mounted separately from ``router`` on purpose. The panel-token dependency takes
# a ``Request``, which a WebSocket route cannot supply; and this route has its own
# gate anyway (same-origin, plus the panel token in the query when one is set,
# because ``new WebSocket()`` cannot send headers).

agent_ws_router = APIRouter(prefix="/api/panel", tags=["panel"])


def _same_origin(websocket: WebSocket) -> bool:
    """True when the page that opened this socket came from this deployment.

    A browser always sends ``Origin`` on a WebSocket handshake, and a page on
    another site can reach ``ws://127.0.0.1:PORT`` just as easily as the panel
    can. Without this check, any web page the operator happens to open could
    drive the assistant — and through it, the registry. No header at all means a
    non-browser client, where the token check is the only gate.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    host = (websocket.headers.get("host") or "").lower()
    return urlsplit(origin).netloc.lower() == host


async def _pump_client_to_upstream(client: WebSocket, upstream: Any) -> None:
    """Forward browser frames upstream until either side goes away."""
    try:
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])
    except Exception:  # noqa: BLE001 - a closed peer is a normal end, not a fault
        return


async def _pump_upstream_to_client(client: WebSocket, upstream: Any) -> None:
    """Forward upstream frames to the browser until either side goes away."""
    try:
        async for raw in upstream:
            if isinstance(raw, (bytes, bytearray)):
                await client.send_bytes(bytes(raw))
            else:
                await client.send_text(raw)
    except Exception:  # noqa: BLE001 - same: this is the disconnect path
        return


@agent_ws_router.websocket("/agent/ws")
async def agent_ws_proxy(websocket: WebSocket) -> None:
    """Carry the aaas socket through this process, adding the bearer token.

    Only mounted when an aaas token is configured — otherwise the page connects
    to that server directly. The panel is still the *caller* in every sense that
    matters: protocol framing, capability execution and event handling all happen
    in the page. This is a byte pump, and the token never leaves this process.
    """
    settings = websocket.app.state.settings
    if not settings.agent_enabled or not settings.aaas_token:
        await websocket.close(code=4404)
        return
    if SUBPROTOCOL not in (websocket.scope.get("subprotocols") or []):
        await websocket.close(code=4400)
        return
    if not _same_origin(websocket):
        await websocket.close(code=4403)
        return
    if settings.panel_token:
        supplied = websocket.query_params.get("token", "")
        # Bytes, not str: `compare_digest` raises TypeError on non-ASCII text, and a
        # query value is attacker-supplied — that would be a 500 instead of a 4401.
        if not secrets.compare_digest(
            supplied.encode("utf-8", "surrogatepass"),
            settings.panel_token.encode("utf-8", "surrogatepass"),
        ):
            await websocket.close(code=4401)
            return

    try:
        import websockets
    except ImportError:  # pragma: no cover - optional dependency
        # No client library, no proxy. The page falls back to "feature absent",
        # which is the honest answer: it cannot reach a token-protected server.
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=SUBPROTOCOL)
    headers = {"Authorization": f"Bearer {settings.aaas_token}"}
    try:
        upstream = await websockets.connect(
            settings.aaas_url,
            subprotocols=[SUBPROTOCOL],
            additional_headers=headers,
            open_timeout=5,
        )
    except TypeError:  # pragma: no cover - websockets < 14
        try:
            upstream = await websockets.connect(
                settings.aaas_url,
                subprotocols=[SUBPROTOCOL],
                extra_headers=headers,
                open_timeout=5,
            )
        except Exception:
            await websocket.close(code=1011, reason="upstream unreachable")
            return
    except Exception:
        await websocket.close(code=1011, reason="upstream unreachable")
        return

    tasks = [
        asyncio.create_task(_pump_client_to_upstream(websocket, upstream)),
        asyncio.create_task(_pump_upstream_to_client(websocket, upstream)),
    ]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        # Cancel and await *both* pumps here, not only on the normal return path:
        # if this handler is cancelled while awaiting `asyncio.wait` (shutdown, or a
        # dropped browser), the tasks would otherwise keep running against closed
        # peers and their exceptions would never be retrieved.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already gone, most likely
            pass

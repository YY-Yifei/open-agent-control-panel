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

import secrets
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
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

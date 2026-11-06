"""Panel API — everything the web UI talks to.

These endpoints are *not* part of the ADP contract: they exist so a human can
see registered agents, submit A2A tasks and follow them. Keeping them under
``/api/panel`` is what lets ``/.well-known`` and ``/v1`` stay exactly the
protocol.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import __version__
from .adp import card as cardmod
from .tasks import TaskNotFound, TaskService

router = APIRouter(prefix="/api/panel", tags=["panel"])


# --------------------------------------------------------------------- security


def require_panel_token(
    request: Request,
    x_panel_token: str | None = Header(default=None, alias="X-Panel-Token"),
    authorization: str | None = Header(default=None),
) -> None:
    """Reject callers without the panel token, when one is configured."""
    expected = request.app.state.settings.panel_token
    if not expected:
        return
    candidate = x_panel_token or ""
    if not candidate and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=401,
            detail="a panel token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


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


class DeleteAgent(BaseModel):
    """The manage token returned when the agent was first registered."""

    token: str = Field(min_length=1, max_length=500)


# ----------------------------------------------------------------------- helpers


def _tasks(request: Request) -> TaskService:
    return request.app.state.tasks


def _registry(request: Request):
    return request.app.state.registry


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
    counts = _tasks(request).agent_counts([item["id"] for item in items])
    fallback = {"taskCount": 0, "activeTaskCount": 0, "lastTaskAt": None}
    for item in items:
        item.update(counts.get(item["id"], fallback))
    return items


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
        "probe": {"enabled": settings.probe_enabled, "interval": settings.probe_interval},
        "panelTokenRequired": bool(settings.panel_token),
        "allowPrivateAgents": settings.allow_private_agents,
        "localMode": settings.local_mode,
        "cardValidation": settings.card_validation,
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
        raise HTTPException(status_code=404, detail=f"no agent with id {agent_id}")

    card = record.get("card") if isinstance(record.get("card"), dict) else {}
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
    }


@router.post("/agents/{agent_id}/refresh")
def refresh_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """Re-fetch the card right now instead of waiting for the probe loop."""
    record = _registry(request).probe(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no agent with id {agent_id}")
    return {"agent": record}


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(request: Request, agent_id: str, payload: DeleteAgent) -> Response:
    """Unregister an agent using the manage token from first registration."""
    _registry(request).delete(agent_id, payload.token)
    return Response(status_code=204)


# ------------------------------------------------------------------------ tasks


@router.post("/agents/{agent_id}/tasks", status_code=201)
def submit_task(request: Request, agent_id: str, payload: SubmitTask) -> dict[str, Any]:
    """Send a message to the agent and record the A2A task it creates."""
    try:
        task = _tasks(request).submit(
            agent_id,
            payload.text,
            context_id=payload.contextId,
            metadata=payload.metadata,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
    try:
        return _tasks(request).list_tasks(
            agent_id=agentId, state=state, q=q, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str) -> dict[str, Any]:
    """One task with its event log and raw payloads."""
    try:
        return {"task": _tasks(request).get(task_id)}
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no task with id {task_id}") from exc


@router.post("/tasks/{task_id}/refresh")
def refresh_task(request: Request, task_id: str) -> dict[str, Any]:
    """Ask the agent for the current task state (``GetTask``)."""
    try:
        return {"task": _tasks(request).refresh(task_id)}
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no task with id {task_id}") from exc
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel")
def cancel_task(request: Request, task_id: str) -> dict[str, Any]:
    """Ask the agent to cancel the task (``CancelTask``)."""
    try:
        return {"task": _tasks(request).cancel(task_id)}
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no task with id {task_id}") from exc
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/messages", status_code=201)
def follow_up(request: Request, task_id: str, payload: FollowUp) -> dict[str, Any]:
    """Send another user message into the same task/context."""
    try:
        task = _tasks(request).follow_up(task_id, payload.text, metadata=payload.metadata)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no task with id {task_id}") from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task": task}


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(request: Request, task_id: str) -> Response:
    """Forget a task locally; the agent keeps its own copy."""
    try:
        _tasks(request).delete(task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no task with id {task_id}") from exc
    return Response(status_code=204)

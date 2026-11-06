"""HTTP surface of the ADP Core contract: five operations, no extensions.

Every route here is defined by ``spec/adp-core.yaml``; panel-specific endpoints
live under ``/api/panel`` and are deliberately kept out of this router so that the
registry contract stays readable on its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Header, Query, Request, Response

from .service import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, RegistryError, RegistryService

router = APIRouter()


def _registry(request: Request) -> RegistryService:
    return request.app.state.registry


def _client_ip(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


@router.get("/.well-known/a2a-registry.json", tags=["discovery"])
def get_registry_card(request: Request) -> dict[str, Any]:
    """Registry self-description (RFC 8615 well-known location)."""
    return request.app.state.settings.registry_card()


@router.post("/v1/agents", tags=["agents"], status_code=201)
def register_agent(
    request: Request,
    response: Response,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Register an agent by URL: fetch, validate, store — synchronously."""
    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        raise RegistryError(400, "BAD_URL", "'url' must be a non-empty string")

    registration = _registry(request).register(url, client_ip=_client_ip(request))
    if registration.created:
        response.status_code = 201
        response.headers["Location"] = f"/v1/agents/{registration.record['id']}"
        body = dict(registration.record)
        body["manageToken"] = registration.manage_token
        return body

    response.status_code = 200
    return registration.record


@router.get("/v1/agents", tags=["agents"])
def list_agents(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    skill: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    changed_since: str | None = Query(default=None, alias="changedSince"),
    full: bool = False,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
) -> dict[str, Any]:
    """Search agents; summaries by default, full records with ``full=true``."""
    return _registry(request).list_agents(
        q=q,
        skill=skill,
        tag=tag,
        status=status,
        changed_since=changed_since,
        full=full,
        limit=limit,
        cursor=cursor,
    )


@router.get("/v1/agents/{agent_id}", tags=["agents"])
def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """One full AgentRecord."""
    record = _registry(request).get(agent_id)
    if record is None:
        raise RegistryError(404, "NOT_FOUND", f"no agent with id {agent_id}")
    return record


@router.delete("/v1/agents/{agent_id}", tags=["agents"], status_code=204)
def delete_agent(
    request: Request,
    agent_id: str,
    x_adp_token: str | None = Header(default=None, alias="X-ADP-Token"),
) -> Response:
    """Unsubscribe; requires the manage token handed out at first registration."""
    _registry(request).delete(agent_id, x_adp_token)
    return Response(status_code=204)

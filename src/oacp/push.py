"""Inbound A2A push notifications — the agent calling *us* back.

A2A's push-notification flow is the only protocol-defined way for an agent to
report a result without being asked: the panel registers ``{url, token}`` with
the agent, and the agent POSTs the whole ``Task`` object to that URL whenever its
state or artifacts change. This module is that URL.

Why it lives at the top level instead of under an existing prefix:

* ``/api/panel/*`` is protected by the panel token, and an agent has no reason
  to hold one — it would be a 401 on every callback;
* ``/v1/*`` is the ADP contract itself, and a task callback is not an ADP
  operation.

So the callback gets its own path, and its credential is the per-task token
inside that path: 24 random bytes minted when the task was submitted, stored on
the task row, and useless for anything else.

The endpoint only ever answers another machine, so its errors are a stable token
plus an HTTP status rather than the panel's localized prose.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .tasks import PushError, TaskService

log = logging.getLogger(__name__)

#: Fallback cap for a callback body when the deployment has not said otherwise.
#: A Task object is usually small, but one that carries an inline image is a
#: perfectly normal A2A answer — refusing it would mean losing whole turns from
#: agents that reply with a picture. ``Settings.max_push_bytes`` is the real knob.
MAX_PUSH_BYTES = 256 * 1024

router = APIRouter(tags=["a2a"])


class _TooLarge(Exception):
    """The body exceeded :data:`MAX_PUSH_BYTES` before it was fully read."""


@router.post("/a2a/push/{token}", include_in_schema=False)
async def receive_push(request: Request, token: str) -> JSONResponse:
    """Accept one task update from an agent.

    Returns 2xx once the payload is stored. A2A lets the agent retry on a
    non-2xx, so ``status`` in the body says what happened: ``applied``,
    ``duplicate`` (a retry of something we already have) or ``ignored`` (a late
    delivery that would move the task backwards).
    """
    limit = _limit(request)
    try:
        raw = await _read_limited(request, limit)
    except _TooLarge:
        return _refuse(413, "payload_too_large", f"callbacks are limited to {limit} bytes")

    try:
        payload: Any = json.loads(raw)
    except ValueError:
        return _refuse(400, "invalid_json", "the callback body is not valid JSON")

    # Be liberal about the envelope: a bare Task is what the spec sends, but a
    # JSON-RPC wrapper costs nothing to unwrap and some implementations send one.
    if isinstance(payload, dict) and "jsonrpc" in payload and "result" in payload:
        payload = payload["result"]
    if not isinstance(payload, dict):
        return _refuse(400, "invalid_task", "the callback body must be a Task object")

    service: TaskService = request.app.state.tasks
    try:
        outcome = service.apply_push(token, payload)
    except PushError as exc:
        log.info("A2A push rejected: %s", exc.reason)
        return _refuse(exc.status, exc.code, exc.reason)

    # A callback is how a conversation learns the answer it was waiting for.
    # This runs *after* the ledger has accepted the update, and it is deliberately
    # unable to fail the delivery: the task is already stored, so a display-layer
    # problem must not make the agent retry forever.
    if outcome.get("status") == "applied":
        sessions = getattr(request.app.state, "sessions", None)
        if sessions is not None:
            try:
                # absorb may download a URL the agent supplied (blocking, up to ten
                # seconds). This is an async handler: calling it inline stalls the
                # event loop for every other request, so it goes to a thread.
                await run_in_threadpool(
                    sessions.absorb_task, str(outcome.get("taskId") or "")
                )
            except Exception:  # noqa: BLE001 — never fail a callback over this
                log.exception("could not fold push %s into its conversation", outcome.get("taskId"))

    log.debug("A2A push %s for %s", outcome.get("status"), outcome.get("taskId"))
    return JSONResponse(outcome, status_code=200)


def _refuse(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": detail}}, status_code=status)


def _limit(request: Request) -> int:
    """The callback body cap for this deployment (settings win over the default)."""
    settings = getattr(request.app.state, "settings", None)
    value = getattr(settings, "max_push_bytes", 0) or 0
    return int(value) if value > 0 else MAX_PUSH_BYTES


async def _read_limited(request: Request, limit: int) -> bytes:
    """Read the body, refusing anything over the cap *while* reading it.

    ``Content-Length`` is checked first so a huge body is refused before a byte
    is buffered; the running total is checked as well, because the header is
    optional and a sender is free to lie about it.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _TooLarge

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)

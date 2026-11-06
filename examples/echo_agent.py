"""A minimal A2A agent for local demos of the control panel.

It serves an AgentCard at ``/.well-known/agent-card.json`` and answers A2A
JSON-RPC calls at ``/a2a`` in both wire dialects (A2A 0.3 and 1.x). It echoes the
text it receives. Nothing here is meant for production: tasks live in memory.

    python examples/echo_agent.py --port 9100
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CARD: dict[str, Any] = {
    "name": "Echo Agent",
    "description": "Repeats whatever it is told, and can report how many messages it got.",
    "version": "0.1.0",
    "url": "http://127.0.0.1:9100/a2a",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "echo.repeat",
            "name": "Repeat text",
            "description": "Returns the input text with an echo prefix.",
            "tags": ["demo", "echo"],
        }
    ],
}

app = FastAPI(title="Echo Agent", docs_url=None, redoc_url=None)

# task id -> {"contextId": str, "messages": list[str], "state": str}
TASKS: dict[str, dict[str, Any]] = {}

METHODS = {
    "message/send": "send",
    "SendMessage": "send",
    "tasks/get": "get",
    "GetTask": "get",
    "tasks/cancel": "cancel",
    "CancelTask": "cancel",
}


def _text_of(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


def _view(task_id: str) -> dict[str, Any]:
    task = TASKS[task_id]
    reply = task["messages"][-1] if task["messages"] else ""
    return {
        "kind": "task",
        "id": task_id,
        "contextId": task["contextId"],
        "status": {
            "state": task["state"],
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "agent", "parts": [{"kind": "text", "text": reply}]},
        },
        "artifacts": [{"parts": [{"kind": "text", "text": reply}]}],
        "history": [],
    }


def _send(message: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    task_id = message.get("taskId") or params.get("id") or f"echo-{uuid.uuid4().hex[:12]}"
    task = TASKS.setdefault(
        task_id,
        {"contextId": message.get("contextId") or uuid.uuid4().hex, "messages": [], "state": "working"},
    )
    if task["state"] == "canceled":
        task["state"] = "working"
    text = _text_of(message)
    task["messages"].append(f"echo: {text}")
    task["state"] = "completed"
    return _view(task_id)


def _get(params: dict[str, Any]) -> dict[str, Any]:
    task_id = params.get("id") or params.get("taskId")
    if task_id not in TASKS:
        raise KeyError(task_id)
    return _view(task_id)


def _cancel(params: dict[str, Any]) -> dict[str, Any]:
    task_id = params.get("id") or params.get("taskId")
    if task_id not in TASKS:
        raise KeyError(task_id)
    TASKS[task_id]["state"] = "canceled"
    return _view(task_id)


@app.get("/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    """Serve the card the registry fetches."""
    return CARD


@app.post("/a2a")
async def a2a(request: Request) -> JSONResponse:
    """Answer one JSON-RPC request."""
    body = await request.json()
    request_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}

    def ok(result: Any) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(code: int, message: str) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    handler = METHODS.get(method)
    if handler is None:
        return error(-32601, f"method not found: {method}")

    try:
        if handler == "send":
            message = params.get("message") or {}
            if not isinstance(message, dict):
                return error(-32602, "params.message must be an object")
            return ok(_send(message, params))
        if handler == "get":
            return ok(_get(params))
        return ok(_cancel(params))
    except KeyError as exc:
        return error(-32001, f"unknown task: {exc.args[0]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the demo A2A echo agent.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args(argv)

    CARD["url"] = f"http://{args.host}:{args.port}/a2a"

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

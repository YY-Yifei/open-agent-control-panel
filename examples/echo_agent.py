"""A minimal A2A agent that registers *itself* with a control panel.

It serves an AgentCard at ``/.well-known/agent-card.json`` and answers A2A JSON-RPC
calls at ``/a2a`` in both wire dialects (A2A 0.3 and 1.x). On startup it pushes its
card URL to a registry, so nobody has to register it by hand:

    python examples/echo_agent.py --port 9100

Start the panel afterwards (or before — the agent retries) and the agent shows up
on its own. Pass ``--no-register`` to keep it a plain, unattached A2A agent.

Registration is the protocol's only write path: an agent says *where its card is*
and the registry fetches it. This example therefore never sends a card body — a
registry that believed a self-reported card would have nothing to verify.

A task is accepted as ``working`` and finishes a moment later; if the caller
registered a callback URL (`capabilities.pushNotifications`), the agent then
``POST``s the whole Task back — A2A push notifications, the one part of the
protocol where the *agent* speaks first.

Nothing here is meant for production: tasks live in memory.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DEFAULT_REGISTRY = "http://127.0.0.1:8000"
CARD_PATH = "/.well-known/agent-card.json"
BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

CARD: dict[str, Any] = {
    "name": "Echo Agent",
    "description": "Repeats whatever it is told, and can report how many messages it got.",
    "version": "0.1.0",
    "url": "http://127.0.0.1:9100/a2a",
    "capabilities": {"streaming": False, "pushNotifications": True},
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

# Kept at module scope so the lifespan hooks below can start and stop it.
KEEPER: "SelfRegistration | None" = None

# task id -> {"contextId": str, "messages": list[str], "state": str, "push": dict | None}
TASKS: dict[str, dict[str, Any]] = {}

METHODS = {
    "message/send": "send",
    "SendMessage": "send",
    "tasks/get": "get",
    "GetTask": "get",
    "tasks/cancel": "cancel",
    "CancelTask": "cancel",
    # Where the caller says "report the result to me".
    "tasks/pushNotificationConfig/set": "set_push",
    "CreateTaskPushNotificationConfig": "set_push",
}


# --------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Registration:
    """What the registry told us about ourselves."""

    agent_id: str
    created: bool
    manage_token: str | None = None
    status: int = 0


class RegistrationError(Exception):
    """The registry answered, but not with a registration."""


def card_url_of(base_url: str) -> str:
    """The URL a registry should fetch to find this agent's card."""
    return base_url.rstrip("/") + CARD_PATH


def register(client: httpx.Client, registry: str, url: str) -> Registration:
    """``POST /v1/agents``: 201 means new, 200 means we were already listed."""
    response = client.post(f"{registry.rstrip('/')}/v1/agents", json={"url": url})
    if response.status_code not in (200, 201):
        raise RegistrationError(_describe(response))
    body = response.json()
    return Registration(
        agent_id=str(body.get("id") or ""),
        created=response.status_code == 201,
        manage_token=body.get("manageToken"),
        status=response.status_code,
    )


def still_registered(client: httpx.Client, registry: str, agent_id: str) -> bool:
    """``GET /v1/agents/{id}``: a 404 means the registry forgot us."""
    response = client.get(f"{registry.rstrip('/')}/v1/agents/{agent_id}")
    if response.status_code == 404:
        return False
    if response.status_code >= 500:
        raise RegistrationError(_describe(response))
    return response.status_code == 200


def deregister(client: httpx.Client, registry: str, agent_id: str, token: str) -> bool:
    """``DELETE /v1/agents/{id}`` with the manage token handed out at 201."""
    response = client.delete(
        f"{registry.rstrip('/')}/v1/agents/{agent_id}",
        headers={"X-ADP-Token": token},
    )
    return response.status_code == 204


def _describe(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        code, message = error.get("code"), error.get("message")
        if code or message:
            return f"{response.status_code} {code or ''} {message or ''}".strip()
    return f"{response.status_code} {response.text[:200]}".strip()


def _say(message: str) -> None:
    """Print a status line that survives being piped into a file or a log drain."""
    print(message, flush=True)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, httpx.RequestError):
        return f"{type(exc).__name__}: {exc}"[:200]
    return str(exc) or type(exc).__name__


class SelfRegistration:
    """Keeps this agent listed on a panel, without ever blocking the server.

    The panel may start after the agent, or be restarted mid-run; neither is fatal.
    Failures back off (1s → 30s) instead of spinning, and the agent keeps serving
    A2A traffic throughout. With a heartbeat the agent only re-registers when the
    registry actually returns 404 — a liveness ``GET`` writes nothing.
    """

    def __init__(
        self,
        registry: str,
        url: str,
        *,
        heartbeat: float = 30.0,
        timeout: float = 5.0,
        deregister_on_exit: bool = False,
        log: Any = None,
    ) -> None:
        self.registry = registry.rstrip("/")
        self.url = url
        self.heartbeat = max(1.0, heartbeat) if heartbeat else 0.0
        self.timeout = timeout
        self.deregister_on_exit = deregister_on_exit
        self.registration: Registration | None = None
        self._log = log or _say
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_note = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "SelfRegistration":
        """Begin registering in the background (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="self-registration", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the loop and, if asked to, unsubscribe using the stored token."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + 1.0)
            self._thread = None
        registration = self.registration
        if self.deregister_on_exit and registration and registration.manage_token:
            try:
                with self._client() as client:
                    removed = deregister(client, self.registry, registration.agent_id, registration.manage_token)
                self._note("unregistered (204)" if removed else "unregister refused by the registry")
            except Exception as exc:  # noqa: BLE001 — never fail on the way out
                self._note(f"could not unregister: {_reason(exc)}")

    def wait_until_registered(self, timeout: float = 10.0) -> Registration | None:
        """Block for tests and scripts; returns the registration or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.registration is not None:
                return self.registration
            if not self._stop.wait(0.02):
                continue
        return self.registration

    # -- internals ---------------------------------------------------------

    def _client(self) -> httpx.Client:
        # trust_env=False: a proxy variable must not hijack a localhost demo.
        return httpx.Client(timeout=self.timeout, trust_env=False)

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self._tick()
                failures, delay = 0, self.heartbeat
                if not delay:  # registered once, no heartbeat requested
                    return
            except Exception as exc:  # noqa: BLE001 — a demo must never die here
                failures += 1
                delay = BACKOFF[min(failures - 1, len(BACKOFF) - 1)]
                self._note(f"registration pending ({_reason(exc)}); retrying in {delay:g}s")
            if self._stop.wait(delay):
                return

    def _tick(self) -> None:
        with self._client() as client:
            if self.registration is None:
                self.registration = register(client, self.registry, self.url)
                verb = "registered" if self.registration.created else "already registered"
                self._note(f"{verb} as {self.registration.agent_id} (HTTP {self.registration.status})")
                return
            if not self.heartbeat:
                return
            if still_registered(client, self.registry, self.registration.agent_id):
                return
            self.registration = register(client, self.registry, self.url)
            self._note(f"re-registered as {self.registration.agent_id} (the registry had dropped us)")

    def _note(self, message: str) -> None:
        # One line per state change, not one per retry.
        if message != self._last_note:
            self._last_note = message
            self._log(f"[self-registration] {message}")


# ------------------------------------------------------------------- A2A server


#: How long a task stays ``working`` before it finishes. The delay is the point:
#: the caller has to have been told where to report *before* the task ends, which
#: is the same window a real agent has.
COMPLETION_DELAY = 0.4

#: Module level so tests can swap in a recorder — a callback otherwise needs a
#: listening socket.
POST = httpx.post


def _text_of(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _view(task_id: str) -> dict[str, Any]:
    """One Task, in the shape both dialects agree on."""
    task = TASKS[task_id]
    reply = task["messages"][-1] if task["messages"] else ""
    done = task["state"] == "completed"
    if done:
        status_text = reply
    elif task["state"] == "canceled":
        status_text = "canceled"
    else:
        status_text = "working on it"
    view: dict[str, Any] = {
        "kind": "task",
        "id": task_id,
        "contextId": task["contextId"],
        "status": {
            "state": task["state"],
            "timestamp": _now(),
            "message": {"role": "agent", "parts": [{"kind": "text", "text": status_text}]},
        },
        "history": [],
    }
    if done and reply:
        # A ``working`` task has no answer yet — which is exactly why the panel
        # cannot just read this view and needs the callback instead.
        view["artifacts"] = [{"parts": [{"kind": "text", "text": reply}]}]
    return view


def _push_config_of(params: dict[str, Any]) -> dict[str, Any] | None:
    """Where an inline push config may ride, in either dialect."""
    configuration = params.get("configuration")
    if not isinstance(configuration, dict):
        return None
    for key in ("pushNotificationConfig", "taskPushNotificationConfig"):
        value = configuration.get(key)
        if isinstance(value, dict) and value.get("url"):
            return value
    return None


def _send(message: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    task_id = message.get("taskId") or params.get("id") or f"echo-{uuid.uuid4().hex[:12]}"
    task = TASKS.setdefault(
        task_id,
        {
            "contextId": message.get("contextId") or uuid.uuid4().hex,
            "messages": [],
            "state": "working",
            "push": None,
        },
    )
    config = _push_config_of(params)
    if config is not None:
        task["push"] = config
    task["state"] = "working"
    task["messages"].append(f"echo: {_text_of(message)}")
    # Snapshot the accepted view *before* the thread starts: the reply must say
    # "working" even when the delay is zero, and a view built afterwards could
    # already show the completion it was supposed to precede.
    accepted = _view(task_id)
    threading.Thread(target=_finish, args=(task_id,), name=f"echo-{task_id}", daemon=True).start()
    return accepted


def _finish(task_id: str) -> None:
    """Finish the task after a beat, then report it to whoever asked."""
    time.sleep(max(0.0, COMPLETION_DELAY))
    task = TASKS.get(task_id)
    if task is None or task["state"] != "working":
        return  # canceled, or finished already: a late finish must not resurrect it
    task["state"] = "completed"
    _notify(task_id)


def _notify(task_id: str) -> None:
    """POST the whole Task to the callback URL — A2A push notifications.

    Fire-and-forget on purpose: the panel answers 2xx once it has stored the task,
    and a callback that fails is a log line, never a failed task.
    """
    config = (TASKS.get(task_id) or {}).get("push") or {}
    url = config.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return
    headers = {"Content-Type": "application/json", "User-Agent": "echo-agent"}
    token = config.get("token")
    if isinstance(token, str) and token:
        # A2A's own header name; the panel also carries the token in the URL path.
        headers["X-A2A-Notification-Token"] = token
    try:
        response = POST(url, json=_view(task_id), headers=headers, timeout=5.0, trust_env=False)
    except Exception as exc:  # noqa: BLE001 — a demo must never die on a callback
        _say(f"[push] could not report {task_id}: {_reason(exc)}")
        return
    _say(f"[push] reported {task_id} -> HTTP {getattr(response, 'status_code', '?')}")


def _set_push(params: dict[str, Any]) -> dict[str, Any]:
    """``tasks/pushNotificationConfig/set`` / ``CreateTaskPushNotificationConfig``."""
    task_id = params.get("taskId") or params.get("id")
    if task_id not in TASKS:
        raise KeyError(task_id)
    config = (
        params.get("pushNotificationConfig")
        or params.get("taskPushNotificationConfig")
        or params.get("config")
    )
    if not isinstance(config, dict) or not config.get("url"):
        raise ValueError("pushNotificationConfig.url is required")
    TASKS[task_id]["push"] = config
    return config


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


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """The server is listening when this runs — so registering here cannot race."""
    if KEEPER is not None:
        KEEPER.start()
    yield
    if KEEPER is not None:
        KEEPER.stop()


app = FastAPI(title="Echo Agent", docs_url=None, redoc_url=None, lifespan=_lifespan)


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
        if handler == "set_push":
            return ok(_set_push(params))
        return ok(_cancel(params))
    except ValueError as exc:
        return error(-32602, str(exc))
    except KeyError as exc:
        return error(-32001, f"unknown task: {exc.args[0]}")


def _reachable_host(host: str) -> str:
    """A wildcard bind is not an address anyone else can dial."""
    if host in {"0.0.0.0", ""}:
        return "127.0.0.1"
    if host in {"::", "[::]"}:
        return "[::1]"
    return host


def main(argv: list[str] | None = None) -> int:
    global KEEPER

    parser = argparse.ArgumentParser(description="Run the demo A2A echo agent.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument(
        "--registry",
        default=os.environ.get("OACP_REGISTRY", DEFAULT_REGISTRY),
        help=f"control panel base URL (default: {DEFAULT_REGISTRY})",
    )
    parser.add_argument("--advertise", default=None, help="base URL the panel should use for this agent")
    parser.add_argument("--no-register", action="store_true", help="stay a plain agent; never call the panel")
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=30.0,
        help="seconds between liveness checks; 0 registers once and stops (default: 30)",
    )
    parser.add_argument("--deregister-on-exit", action="store_true", help="DELETE the entry on shutdown")
    args = parser.parse_args(argv)

    advertise = (args.advertise or f"http://{_reachable_host(args.host)}:{args.port}").rstrip("/")
    CARD["url"] = f"{advertise}/a2a"

    if args.no_register:
        KEEPER = None
        _say("not registering: --no-register")
    else:
        KEEPER = SelfRegistration(
            args.registry,
            card_url_of(advertise),
            heartbeat=args.heartbeat,
            deregister_on_exit=args.deregister_on_exit,
        )
        _say(f"will register with {args.registry} as {card_url_of(advertise)}")

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:  # pragma: no cover — uvicorn normally handles it
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

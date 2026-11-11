"""The panel body cap must bite *before* FastAPI parses the JSON."""

import asyncio
import json

from oacp.app import PANEL_BODY_LIMIT, _BodyLimit, _body_limit
from oacp.config import Settings


def _drive(limit, chunks, declared=None, path="/api/panel/sessions"):
    """Run the middleware over a fake ASGI scope; return (status, body bytes)."""
    sent = []
    pending = list(chunks)

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": bool(pending)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", str(declared).encode()))
    scope = {"type": "http", "path": path, "headers": headers}

    asyncio.run(_BodyLimit(downstream, limit)(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], body


def test_declared_length_over_the_cap_is_refused_without_reading():
    status, body = _drive(1024, [b"{}"], declared=2 * 1024 * 1024)
    assert status == 413
    payload = json.loads(body)
    assert payload["code"] == "payload_too_large"
    assert payload["detail"]


def test_streamed_bytes_over_the_cap_are_refused_without_a_length_header():
    # Two chunks that only exceed the cap together: the running total must catch it.
    status, body = _drive(1024, [b"x" * 700, b"y" * 700])
    assert status == 413
    assert json.loads(body)["code"] == "payload_too_large"


def test_a_body_under_the_cap_passes_through():
    status, body = _drive(1024, [b"x" * 100])
    assert status == 200
    assert body == b"ok"


def test_the_cap_is_never_tighter_than_the_deployment_image_budget():
    settings = Settings(max_image_bytes=30 * 1024 * 1024, max_images_per_message=4)
    assert _body_limit(settings) >= 30 * 1024 * 1024 * 4
    assert _body_limit(Settings()) >= PANEL_BODY_LIMIT


def test_fastapi_cannot_swallow_the_cap_into_a_generic_400():
    # FastAPI wraps body parsing in `except Exception` and answers 400 "There was an
    # error parsing the body". If the cap raised an ordinary Exception, the counted
    # path would never reach the client as the panel's own 413 — the unit test above
    # cannot see that, because its downstream is not FastAPI. This one is.
    from fastapi import Body, FastAPI

    inner = FastAPI()

    @inner.post("/api/panel/echo")
    def echo(payload: dict = Body(...)) -> dict:
        return payload

    wrapped = _BodyLimit(inner, limit=64)
    body = b'{"a": "' + b"x" * 200 + b'"}'
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/panel/echo",
        "raw_path": b"/api/panel/echo",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            # Understated on purpose: only the running total can catch this body.
            (b"content-length", b"10"),
        ],
        "client": ("test", 1234),
        "server": ("test", 80),
    }
    sent = []
    pending = [body]

    async def receive():
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(wrapped(scope, receive, send))
    assert sent[0]["status"] == 413, sent[0]
    payload = json.loads(
        b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    )
    assert payload["code"] == "payload_too_large"
    assert payload["detail"]


def test_the_adp_surface_keeps_a_cap_of_its_own() -> None:
    # `/v1/agents` is unauthenticated and takes `{"url": ...}`; letting it inherit
    # the panel's image-derived cap would make the registry parse tens of megabytes
    # for a request whose real body is a few hundred bytes.
    status, body = _drive(
        200 * 1024 * 1024, [b"x" * 64], declared=2 * 1024 * 1024, path="/v1/agents"
    )
    assert status == 413
    assert json.loads(body)["code"] == "PAYLOAD_TOO_LARGE"


def test_the_adp_surface_answers_in_its_own_shape() -> None:
    # `/v1` is a published contract: `RegistryError.body()` renders `{code, message}`
    # in English. A localized panel body here would be a wire change nobody asked for.
    status, body = _drive(1024, [b"{}"], declared=2 * 1024 * 1024, path="/v1/agents")
    payload = json.loads(body)
    assert status == 413
    assert payload["code"] == "PAYLOAD_TOO_LARGE"
    assert payload["message"]
    assert "detail" not in payload

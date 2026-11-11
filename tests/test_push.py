"""A2A push notifications: the agent calling the panel back with a task update.

The transport is mocked, so "the agent" is the handler in :func:`make_transport`:
it answers the send with a non-terminal task, accepts (or refuses) the push
registration, and then the test plays the agent's role by POSTing a callback to
the URL the panel handed out — exactly what a real agent would do.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeFetcher, make_card
from oacp.app import create_app
from oacp.config import Settings

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"
PANEL_URL = "https://panel.example.com"

PUSH_METHODS = {"tasks/pushNotificationConfig/set", "CreateTaskPushNotificationConfig"}
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode("ascii")
GET_METHODS = {"tasks/get", "GetTask"}


def task_payload(
    task_id: str = "task-1",
    state: str = "working",
    text: str = "still working",
    *,
    artifacts: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "task",
        "id": task_id,
        "contextId": "ctx-1",
        "status": {"state": state, "message": {"role": "agent", "parts": [{"kind": "text", "text": text}]}},
        "history": [],
    }
    if artifacts:
        payload["artifacts"] = [{"parts": [{"kind": "text", "text": text}]}]
    return payload


def make_transport(
    recorded: list[dict[str, Any]],
    *,
    send_state: str = "working",
    get_state: str = "completed",
    push_set_ok: bool = True,
) -> httpx.MockTransport:
    """A fake agent that answers every A2A method the panel may send."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        recorded.append(body)
        method = body["method"]

        def ok(result: Any) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        def unknown() -> httpx.Response:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "method not found"}},
            )

        if method in {"message/send", "SendMessage"}:
            return ok(task_payload(state=send_state))
        if method in PUSH_METHODS:
            return ok({}) if push_set_ok else unknown()
        if method in GET_METHODS:
            return ok(task_payload(state=get_state))
        return unknown()

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------- fixtures


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """The panel with push notifications switched on and a public address."""
    return Settings(
        db_path=str(tmp_path / "oacp.db"),
        probe_enabled=False,
        allow_private_agents=True,
        register_per_hour=1000,
        a2a_timeout=5.0,
        push_enabled=True,
        public_url=PANEL_URL,
    )


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fetcher(app) -> FakeFetcher:
    fake = FakeFetcher()
    app.state.registry.fetcher = fake
    return fake


# ----------------------------------------------------------------------- helpers


def prepare(
    client: TestClient,
    app,
    fetcher: FakeFetcher,
    *,
    push_capable: bool = True,
    send_state: str = "working",
    get_state: str = "completed",
    push_set_ok: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Register an agent, submit one task, and return (agent, task, sent bodies)."""
    fetcher.cards[CARD_URL] = make_card(
        capabilities={"streaming": False, "pushNotifications": push_capable}
    )
    agent = client.post("/v1/agents", json={"url": CARD_URL}).json()
    recorded: list[dict[str, Any]] = []
    app.state.a2a.transport = make_transport(
        recorded, send_state=send_state, get_state=get_state, push_set_ok=push_set_ok
    )
    task = client.post(
        f"/api/panel/agents/{agent['id']}/tasks", json={"text": "long job"}
    ).json()["task"]
    return agent, task, recorded


def token_of(app, task_id: str) -> str:
    with app.state.db.connect() as connection:
        row = connection.execute(
            "SELECT push_token FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row is not None and row["push_token"], "no callback token was minted"
    return row["push_token"]


def callback(client: TestClient, token: str, payload: Any) -> httpx.Response:
    return client.post(f"/a2a/push/{token}", json=payload)


# ---------------------------------------------------------------------- the happy path


def test_running_task_is_offered_a_callback_url(client, app, fetcher) -> None:
    _agent, task, recorded = prepare(client, app, fetcher)

    assert task["state"] == "working"
    assert task["pushState"] == "registered"

    # The config rode inline with the message, and the follow-up `set` confirmed it.
    inline = recorded[0]["params"]["configuration"]["pushNotificationConfig"]
    assert inline["url"].startswith(f"{PANEL_URL}/a2a/push/")
    assert inline["token"] == token_of(app, task["id"])

    assert recorded[1]["method"] == "tasks/pushNotificationConfig/set"
    assert recorded[1]["params"]["taskId"] == "task-1"
    assert recorded[1]["params"]["pushNotificationConfig"]["url"] == inline["url"]


def test_callback_stores_the_agents_result(client, app, fetcher) -> None:
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])

    response = callback(client, token, task_payload(state="completed", text="the answer"))
    assert response.status_code == 200
    assert response.json()["status"] == "applied"

    detail = client.get(f"/api/panel/tasks/{task['id']}").json()["task"]
    assert detail["state"] == "completed"
    assert detail["text"] == "the answer"
    assert detail["pushCount"] == 1
    assert detail["lastPushAt"]
    kinds = [event["kind"] for event in detail["events"]]
    assert kinds == ["submit", "response", "push-register", "push"]


def test_callback_without_artifacts_keeps_the_text_we_have(client, app, fetcher) -> None:
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])

    callback(client, token, task_payload(state="working", text="halfway"))
    # A bare status update: no artifacts, no status message.
    bare = {"id": "task-1", "contextId": "ctx-1", "status": {"state": "working"}}
    assert callback(client, token, bare).json()["status"] == "applied"

    detail = client.get(f"/api/panel/tasks/{task['id']}").json()["task"]
    assert detail["text"] == "halfway", "a sloppy callback must not erase the answer"
    assert detail["artifactCount"] == 1


# --------------------------------------------------------------------- idempotency


def test_repeated_delivery_is_a_duplicate(client, app, fetcher) -> None:
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])

    first = callback(client, token, task_payload(state="completed", text="done"))
    second = callback(client, token, task_payload(state="completed", text="done"))
    assert first.json()["status"] == "applied"
    assert second.json()["status"] == "duplicate"

    detail = client.get(f"/api/panel/tasks/{task['id']}").json()["task"]
    assert detail["state"] == "completed"
    assert detail["pushCount"] == 2, "we still count every delivery we received"
    kinds = [event["kind"] for event in detail["events"]]
    assert kinds.count("push") == 1
    assert "push-duplicate" in kinds


def test_late_delivery_cannot_move_the_task_backwards(client, app, fetcher) -> None:
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])

    callback(client, token, task_payload(state="completed", text="done"))
    late = callback(client, token, task_payload(state="working", text="older update"))

    assert late.status_code == 200
    assert late.json()["status"] == "ignored"

    detail = client.get(f"/api/panel/tasks/{task['id']}").json()["task"]
    assert detail["state"] == "completed"
    assert detail["text"] == "done"
    assert "push-ignored" in [event["kind"] for event in detail["events"]]


# ------------------------------------------------------------------------ refusals


def test_unknown_callback_token_is_not_found(client) -> None:
    response = callback(client, "not-a-real-token", task_payload())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_callback_token"


def test_callback_for_another_task_is_refused(client, app, fetcher) -> None:
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])

    response = callback(client, token, task_payload(task_id="someone-elses-task"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "task_mismatch"


def test_body_that_is_not_a_task_is_refused(client, app, fetcher) -> None:
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])

    assert callback(client, token, {"hello": "world"}).status_code == 400
    assert client.post(f"/a2a/push/{token}", content=b"not json").status_code == 400
    assert client.post(f"/a2a/push/{token}", json=["a", "list"]).status_code == 400


def test_oversized_callback_is_refused(client, app, fetcher, monkeypatch) -> None:
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])
    # The cap is a deployment setting, not a constant: the default has to be large
    # enough for a callback that carries an inline image, which is a normal A2A
    # answer. So the test shrinks the setting rather than the module default.
    monkeypatch.setattr(app.state.settings, "max_push_bytes", 64)

    response = client.post(f"/a2a/push/{token}", json=task_payload())
    assert response.status_code == 413
    assert "64" in response.json()["error"]["message"]


# ------------------------------------------------------------------ capability gate


def test_agent_without_the_capability_is_never_asked_for_one(client, app, fetcher) -> None:
    _agent, task, recorded = prepare(client, app, fetcher, push_capable=False)

    assert task["pushState"] == "unsupported"
    assert "configuration" not in recorded[0]["params"]
    assert all(item["method"] not in PUSH_METHODS for item in recorded)
    with app.state.db.connect() as connection:
        row = connection.execute(
            "SELECT push_token FROM tasks WHERE id = ?", (task["id"],)
        ).fetchone()
    assert row["push_token"] is None


def test_finished_task_is_settled_not_followed(client, app, fetcher) -> None:
    _agent, task, recorded = prepare(client, app, fetcher, send_state="completed")

    assert task["state"] == "completed"
    assert task["pushState"] == "settled"
    assert all(item["method"] not in PUSH_METHODS for item in recorded)


def test_refused_registration_falls_back_to_inline_and_catches_up(client, app, fetcher) -> None:
    """When `set` is not implemented, ask once — the task may already be done."""
    _agent, task, recorded = prepare(client, app, fetcher, push_set_ok=False)

    assert task["pushState"] == "inline"
    assert task["state"] == "completed", "the catch-up GetTask should have finished the story"
    methods = [item["method"] for item in recorded]
    assert "tasks/pushNotificationConfig/set" in methods
    assert "CreateTaskPushNotificationConfig" in methods, "both dialects were tried"
    assert any(method in GET_METHODS for method in methods)


# ------------------------------------------------------------------------- wiring


def test_push_disabled_means_nothing_is_offered(tmp_path) -> None:
    quiet = Settings(
        db_path=str(tmp_path / "quiet.db"),
        probe_enabled=False,
        allow_private_agents=True,
        register_per_hour=1000,
        a2a_timeout=5.0,
        push_enabled=False,
    )
    app = create_app(quiet)
    fake = FakeFetcher()
    fake.cards[CARD_URL] = make_card(
        capabilities={"streaming": False, "pushNotifications": True}
    )
    app.state.registry.fetcher = fake
    recorded: list[dict[str, Any]] = []
    app.state.a2a.transport = make_transport(recorded)

    with TestClient(app) as client:
        agent = client.post("/v1/agents", json={"url": CARD_URL}).json()
        task = client.post(
            f"/api/panel/agents/{agent['id']}/tasks", json={"text": "hello"}
        ).json()["task"]

    assert task["pushState"] is None
    assert "configuration" not in recorded[0]["params"]
    assert all(item["method"] not in PUSH_METHODS for item in recorded)


def test_summary_reports_whether_push_is_on(client) -> None:
    assert client.get("/api/panel/summary").json()["pushEnabled"] is True


# -------------------------------------------------------------- default deployment


def test_push_is_offered_by_default(tmp_path) -> None:
    """No flag, no environment: a plain panel still offers callbacks."""
    settings = Settings(db_path=str(tmp_path / "default.db"), probe_enabled=False)
    assert settings.push_enabled is True
    assert settings.push_callback_base == "http://127.0.0.1:8000/a2a/push"


def test_a_default_panel_offers_a_callback_a_local_agent_can_reach(tmp_path) -> None:
    """The whole loop, with default settings: mint, offer, accept the callback."""
    settings = Settings(
        db_path=str(tmp_path / "local.db"),
        probe_enabled=False,
        allow_private_agents=True,  # what local mode does for a loopback bind
        register_per_hour=1000,
        a2a_timeout=5.0,
    )
    app = create_app(settings)
    fetcher = FakeFetcher()
    fetcher.cards[CARD_URL] = make_card(
        capabilities={"streaming": False, "pushNotifications": True}
    )
    app.state.registry.fetcher = fetcher
    recorded: list[dict[str, Any]] = []
    app.state.a2a.transport = make_transport(recorded)

    with TestClient(app) as client:
        agent = client.post("/v1/agents", json={"url": CARD_URL}).json()
        task = client.post(
            f"/api/panel/agents/{agent['id']}/tasks", json={"text": "long job"}
        ).json()["task"]

        assert task["pushState"] == "registered"
        inline = recorded[0]["params"]["configuration"]["pushNotificationConfig"]
        assert inline["url"].startswith("http://127.0.0.1:8000/a2a/push/")

        # The URL the panel just handed out is the one that works.
        reply = client.post(
            f"/a2a/push/{inline['token']}",
            json=task_payload(state="completed", text="done"),
        )
        assert reply.status_code == 200
        assert reply.json()["status"] == "applied"
        assert client.get(f"/api/panel/tasks/{task['id']}").json()["task"]["state"] == "completed"


def test_a_retried_status_update_after_a_picture_is_a_duplicate(client, app, fetcher) -> None:
    # Once a picture is stored, a callback that omits `artifacts` (a status-only
    # update, the sloppy delivery `_merge_task` keeps the old answer for) used to
    # compare an empty signature against a non-empty one and read as "news" on
    # every retry: `applied` forever, one `push` event per delivery.
    _agent, task, _recorded = prepare(client, app, fetcher)
    token = token_of(app, task["id"])

    pictures = task_payload(state="working", text="", artifacts=False)
    pictures["artifacts"] = [
        {
            "parts": [
                {
                    "kind": "file",
                    "file": {
                        "bytes": PNG_B64,
                        "mimeType": "image/png",
                        "name": "chart.png",
                    },
                }
            ]
        }
    ]
    assert callback(client, token, pictures).json()["status"] == "applied"

    bare = {"kind": "task", "id": "task-1", "contextId": "ctx-1", "status": {"state": "working"}}
    assert callback(client, token, bare).json()["status"] == "duplicate"

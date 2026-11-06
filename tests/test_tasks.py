"""Task submission and follow-up, with a mock A2A transport on the client."""

from __future__ import annotations

import json

import httpx

from conftest import make_card

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"


def task_payload(task_id: str = "task-1", state: str = "completed", text: str = "done") -> dict:
    return {
        "kind": "task",
        "id": task_id,
        "contextId": "ctx-1",
        "status": {"state": state},
        "artifacts": [{"parts": [{"kind": "text", "text": text}]}],
        "history": [],
    }


def make_transport(
    recorded: list[dict],
    *,
    state: str = "completed",
    text: str = "done",
    fail_legacy_send: bool = False,
    http_status: int | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        recorded.append(body)
        if http_status is not None:
            return httpx.Response(http_status, json={"error": "boom"})
        method = body["method"]
        if fail_legacy_send and method == "message/send":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
        if method in {"message/send", "SendMessage"}:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": task_payload(state=state, text=text)},
            )
        if method in {"tasks/get", "GetTask"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task_payload(state="working", text="still working"),
                },
            )
        if method in {"tasks/cancel", "CancelTask"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task_payload(state="canceled", text="stopped"),
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "unknown"}},
        )

    return httpx.MockTransport(handler)


def prepare_agent(client, app, fetcher):
    fetcher.cards[CARD_URL] = make_card()
    created = client.post("/v1/agents", json={"url": CARD_URL}).json()
    recorded: list[dict] = []
    app.state.a2a.transport = make_transport(recorded)
    return created, recorded


def test_submit_task_records_the_result(client, app, fetcher) -> None:
    agent, recorded = prepare_agent(client, app, fetcher)

    response = client.post(
        f"/api/panel/agents/{agent['id']}/tasks",
        json={"text": "Where is Lisbon?"},
    )
    assert response.status_code == 201
    task = response.json()["task"]
    assert task["state"] == "completed"
    assert task["remoteTaskId"] == "task-1"
    assert task["contextId"] == "ctx-1"
    assert task["text"] == "done"
    assert task["agentId"] == agent["id"]

    sent = recorded[0]
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "message/send"
    message = sent["params"]["message"]
    assert message["role"] == "user"
    assert message["parts"] == [{"kind": "text", "text": "Where is Lisbon?"}]
    assert message["messageId"]

    detail = client.get(f"/api/panel/tasks/{task['id']}").json()["task"]
    assert [event["kind"] for event in detail["events"]] == ["submit", "response"]
    assert detail["request"]["text"] == "Where is Lisbon?"
    assert detail["response"]["id"] == "task-1"


def test_follow_up_continues_the_same_task(client, app, fetcher) -> None:
    agent, recorded = prepare_agent(client, app, fetcher)
    task = client.post(
        f"/api/panel/agents/{agent['id']}/tasks", json={"text": "first"}
    ).json()["task"]

    response = client.post(
        f"/api/panel/tasks/{task['id']}/messages",
        json={"text": "second"},
    )
    assert response.status_code == 201
    updated = response.json()["task"]
    assert updated["messageCount"] == 2
    assert updated["contextId"] == "ctx-1"
    assert recorded[-1]["params"]["message"]["taskId"] == "task-1"
    assert recorded[-1]["params"]["message"]["contextId"] == "ctx-1"


def test_refresh_and_cancel_talk_to_the_agent(client, app, fetcher) -> None:
    agent, recorded = prepare_agent(client, app, fetcher)
    task = client.post(
        f"/api/panel/agents/{agent['id']}/tasks", json={"text": "status please"}
    ).json()["task"]

    refreshed = client.post(f"/api/panel/tasks/{task['id']}/refresh").json()["task"]
    assert refreshed["state"] == "working"
    assert refreshed["text"] == "still working"
    assert recorded[-1]["method"] == "tasks/get"

    canceled = client.post(f"/api/panel/tasks/{task['id']}/cancel").json()["task"]
    assert canceled["state"] == "canceled"
    assert recorded[-1]["method"] == "tasks/cancel"
    assert recorded[-1]["params"] == {"id": "task-1"}


def test_client_falls_back_to_v1_method_names(client, app, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    agent = client.post("/v1/agents", json={"url": CARD_URL}).json()
    recorded: list[dict] = []
    app.state.a2a.transport = make_transport(recorded, fail_legacy_send=True)

    task = client.post(
        f"/api/panel/agents/{agent['id']}/tasks", json={"text": "hello"}
    ).json()["task"]
    assert task["state"] == "completed"

    methods = [item["method"] for item in recorded]
    assert methods[:2] == ["message/send", "SendMessage"]
    assert recorded[1]["params"]["message"]["role"] == "ROLE_USER"
    assert recorded[1]["params"]["message"]["parts"] == [{"text": "hello"}]


def test_transport_failure_is_recorded_on_the_task(client, app, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    agent = client.post("/v1/agents", json={"url": CARD_URL}).json()
    app.state.a2a.transport = make_transport([], http_status=500)

    task = client.post(
        f"/api/panel/agents/{agent['id']}/tasks", json={"text": "will fail"}
    ).json()["task"]
    assert task["state"] == "error"
    assert "HTTP 500" in task["error"]


def test_task_listing_filters_and_delete(client, app, fetcher) -> None:
    agent, _recorded = prepare_agent(client, app, fetcher)
    first = client.post(f"/api/panel/agents/{agent['id']}/tasks", json={"text": "alpha"}).json()["task"]
    second = client.post(f"/api/panel/agents/{agent['id']}/tasks", json={"text": "beta"}).json()["task"]

    listing = client.get("/api/panel/tasks").json()["items"]
    assert len(listing) == 2

    active = client.get("/api/panel/tasks", params={"state": "active"}).json()["items"]
    assert active == []

    completed = client.get("/api/panel/tasks", params={"state": "completed"}).json()["items"]
    assert {item["id"] for item in completed} == {first["id"], second["id"]}

    assert client.delete(f"/api/panel/tasks/{first['id']}").status_code == 204
    assert client.get(f"/api/panel/tasks/{first['id']}").status_code == 404


def test_submit_to_unknown_agent_returns_404(client) -> None:
    response = client.post(
        "/api/panel/agents/a_0000000000000000/tasks", json={"text": "hello"}
    )
    assert response.status_code == 404


def test_summary_counts_agents_and_tasks(client, app, fetcher) -> None:
    agent, _recorded = prepare_agent(client, app, fetcher)
    client.post(f"/api/panel/agents/{agent['id']}/tasks", json={"text": "count me"})

    summary = client.get("/api/panel/summary").json()
    assert summary["agents"]["total"] == 1
    assert summary["agents"]["ok"] == 1
    assert summary["tasks"]["total"] == 1
    assert summary["tasks"]["byState"]["completed"] == 1
    assert summary["probe"]["enabled"] is False

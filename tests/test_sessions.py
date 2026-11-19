"""Conversations end to end: the panel's model, a scripted agent underneath."""

from __future__ import annotations

import json

import httpx

from conftest import make_card

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"
PUSH_CARD_URL = "https://pusher.example.com/.well-known/agent-card.json"


def task_payload(
    task_id: str,
    state: str,
    text: str = "",
    context: str = "ctx-1",
    status_text: str = "",
) -> dict:
    """An A2A Task in the 0.3 spelling.

    ``text`` becomes an artifact (the answer); ``status_text`` becomes
    ``status.message`` (what the agent is doing, or what it is asking).
    """
    status: dict = {"state": state}
    if status_text:
        status["message"] = {"role": "agent", "parts": [{"kind": "text", "text": status_text}]}
    payload: dict = {
        "kind": "task",
        "id": task_id,
        "contextId": context,
        "status": status,
        "history": [],
    }
    if text:
        payload["artifacts"] = [{"parts": [{"kind": "text", "text": text}]}]
    return payload


def _ok(body: dict, result: object) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _error(body: dict, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": code, "message": message}}
    )


class ScriptedAgent:
    """One agent, scripted: what it answers to a send, a get and a cancel."""

    def __init__(self) -> None:
        self.recorded: list[dict] = []
        self.send_state = "completed"
        self.send_text = "done"
        self.send_status = ""
        self.get_state = "working"
        self.get_text = ""
        self.get_status = ""
        self._turns = 0

    def install(self, app) -> "ScriptedAgent":
        app.state.a2a.transport = httpx.MockTransport(self._handle)
        return self

    def last_message(self) -> dict:
        """The A2A Message of the most recent send."""
        for body in reversed(self.recorded):
            if body["method"] in {"message/send", "SendMessage"}:
                return body["params"]["message"]
        raise AssertionError("no send was recorded")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.recorded.append(body)
        method = body["method"]

        if method in {"message/send", "SendMessage"}:
            message = body["params"].get("message") or {}
            task_id = message.get("taskId")
            if not task_id:
                self._turns += 1
                task_id = f"task-{self._turns}"
            return _ok(
                body,
                task_payload(
                    task_id, self.send_state, self.send_text, status_text=self.send_status
                ),
            )
        if method in {"tasks/get", "GetTask"}:
            return _ok(
                body,
                task_payload(
                    body["params"]["id"],
                    self.get_state,
                    self.get_text,
                    status_text=self.get_status,
                ),
            )
        if method in {"tasks/cancel", "CancelTask"}:
            return _ok(body, task_payload(body["params"]["id"], "canceled", "stopped"))
        if method in {"tasks/pushNotificationConfig/set", "CreateTaskPushNotificationConfig"}:
            return _ok(body, {"taskId": body["params"]["taskId"]})
        return _error(body, -32601, f"method not found: {method}")


def register(client, fetcher, *, url: str = CARD_URL, **overrides) -> dict:
    """Register an agent served by the fake fetcher and return its record."""
    fetcher.cards[url] = make_card(**overrides)
    return client.post("/v1/agents", json={"url": url}).json()


def open_session(client, agent_id: str, text: str) -> dict:
    response = client.post(f"/api/panel/agents/{agent_id}/sessions", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()["session"]


# ------------------------------------------------------------------ the basics


def test_a_conversation_records_both_sides(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    ScriptedAgent().install(app)

    session = open_session(client, agent["id"], "Where is Lisbon?")

    assert session["state"] == "done"
    assert session["title"] == "Where is Lisbon?"
    assert session["agentName"] == "Geo Agent"
    assert session["messageCount"] == 2
    assert [(m["role"], m["text"]) for m in session["messages"]] == [
        ("user", "Where is Lisbon?"),
        ("agent", "done"),
    ]


def test_the_next_turn_starts_a_new_task_in_the_same_context(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent().install(app)
    session = open_session(client, agent["id"], "Where is Lisbon?")

    second = client.post(
        f"/api/panel/sessions/{session['id']}/messages", json={"text": "And Porto?"}
    ).json()["session"]

    assert len(second["messages"]) == 4
    message = fake.last_message()
    # A finished task cannot take another message, so this is a fresh turn —
    # but the same context, which is what gives the conversation continuity.
    assert "taskId" not in message
    assert message["contextId"] == "ctx-1"


def test_a_running_turn_takes_the_follow_up_itself(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = ""
    fake.install(app)
    session = open_session(client, agent["id"], "Long job")

    assert session["state"] == "working"
    assert [m["role"] for m in session["messages"]] == ["user"]

    client.post(f"/api/panel/sessions/{session['id']}/messages", json={"text": "keep going"})

    message = fake.last_message()
    assert message["taskId"] == "task-1"
    assert message["contextId"] == "ctx-1"


def test_states_the_panel_shows_are_only_its_own_words(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "input-required"
    fake.send_text = "which city?"
    fake.install(app)

    session = open_session(client, agent["id"], "Plan a trip")
    assert session["state"] == "waiting"

    fake.send_state = "completed"
    done = client.post(
        f"/api/panel/sessions/{session['id']}/messages", json={"text": "Lisbon"}
    ).json()["session"]
    assert done["state"] == "done"


# --------------------------------------------------------------------- syncing


def test_sync_brings_back_the_answer(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.install(app)
    session = open_session(client, agent["id"], "Where is Lisbon?")

    fake.get_state = "completed"
    fake.get_text = "Lisbon is in Portugal"
    synced = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    assert synced["state"] == "done"
    assert synced["messages"][-1]["text"] == "Lisbon is in Portugal"


def test_a_repeated_status_is_progress_not_a_message(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = ""
    fake.get_state = "working"
    fake.get_status = "working on it"
    fake.install(app)
    session = open_session(client, agent["id"], "Long job")

    for _ in range(3):
        session = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # 同一句状态重复三拍：对话里一条都不该多出来，它只属于「正在处理…」那一行。
    assert [m["text"] for m in session["messages"]] == ["Long job"]
    assert session["progress"]["text"] == "working on it"


def test_progress_updates_replace_the_pending_line(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = ""
    fake.send_status = "reading index.html"
    fake.install(app)
    session = open_session(client, agent["id"], "Build the site")

    assert session["progress"]["text"] == "reading index.html"

    for step in ("writing styles.css", "running the tests"):
        fake.get_state = "working"
        fake.get_status = step
        session = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # 三句不同的进度，一行都不是回答：对话里仍然只有用户那句话。
    assert [m["text"] for m in session["messages"]] == ["Build the site"]
    assert session["progress"]["text"] == "running the tests"


def test_a_silent_snapshot_keeps_the_progress_it_had(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = ""
    fake.send_status = "reading index.html"
    fake.install(app)
    session = open_session(client, agent["id"], "Build the site")

    fake.get_state = "working"
    fake.get_status = ""
    session = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # 这一次没带话，不等于进度倒退：不清空，否则气泡会闪。
    assert session["progress"]["text"] == "reading index.html"


def test_a_finished_turn_without_artifacts_promotes_its_status(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "completed"
    fake.send_text = ""
    fake.send_status = "Lisbon is in Portugal"
    fake.install(app)

    session = open_session(client, agent["id"], "Where is Lisbon?")

    # 纯文本 agent 把答案放在 status.message 里：丢掉它就是空白会话。
    assert session["state"] == "done"
    assert [m["text"] for m in session["messages"]] == [
        "Where is Lisbon?",
        "Lisbon is in Portugal",
    ]
    assert session["progress"] is None


def test_a_finished_turn_keeps_its_artifacts_over_the_status(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "completed"
    fake.send_text = "Lisbon is in Portugal"
    fake.send_status = "all done"
    fake.install(app)

    session = open_session(client, agent["id"], "Where is Lisbon?")

    # 有 artifacts 时，「all done」不是回答，不该占一条消息。
    assert [m["text"] for m in session["messages"]] == [
        "Where is Lisbon?",
        "Lisbon is in Portugal",
    ]


def test_a_waiting_turn_keeps_the_question_it_asked(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "input-required"
    fake.send_text = ""
    fake.send_status = "which city?"
    fake.install(app)

    session = open_session(client, agent["id"], "Plan a trip")

    assert session["state"] == "waiting"
    assert session["messages"][-1]["text"] == "which city?"
    assert session["progress"] is None, "waiting is not work in progress"


def test_a_question_extends_the_answer_instead_of_repeating_it(
    client, app, fetcher
) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = "the draft"
    fake.install(app)
    session = open_session(client, agent["id"], "Plan a trip")
    assert session["messages"][-1]["text"] == "the draft"

    # 同一份 artifact 现在多配了一个问题：回答已经在屏幕上了，只该把问题接上去。
    fake.get_state = "input-required"
    fake.get_text = "the draft"
    fake.get_status = "which city?"
    session = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    assert [m["text"] for m in session["messages"]] == [
        "Plan a trip",
        "the draft\n\nwhich city?",
    ]
    assert session["messageCount"] == 2, "接上去不是再发一条"


def test_a_question_does_not_duplicate_the_answer_it_was_glued_to(
    client, app, fetcher
) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "input-required"
    fake.send_text = "the answer"
    fake.send_status = "which city?"
    fake.install(app)
    session = open_session(client, agent["id"], "Plan a trip")

    # 停下来的那一拍：回答与问题是同一个气泡里的两段。
    assert session["messages"][-1]["text"] == "the answer\n\nwhich city?"

    fake.get_state = "completed"
    fake.get_text = "the answer"
    fake.get_status = ""
    done = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # 恢复成「只有回答」时，不能因为它与上一句不完全相同就再贴一遍。
    assert [m["text"] for m in done["messages"]] == [
        "Plan a trip",
        "the answer\n\nwhich city?",
    ]


def test_an_unrecognised_state_keeps_its_status_out_of_the_transcript(
    client, app, fetcher
) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = ""
    fake.send_status = "reading index.html"
    fake.install(app)
    session = open_session(client, agent["id"], "Build the site")

    # 一个我们不认识的拼法会落到 idle —— 但它完全可能还在跑，所以 status 仍然是
    # 进度，不是回答。
    fake.get_state = "unknown"
    fake.get_status = "writing styles.css"
    session = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    assert session["state"] == "idle"
    assert [m["text"] for m in session["messages"]] == ["Build the site"]


def test_a_waiting_turn_does_not_inherit_the_running_line(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = ""
    fake.send_status = "reading index.html"
    fake.install(app)
    session = open_session(client, agent["id"], "Build the site")
    assert session["progress"]["text"] == "reading index.html"

    # 转到 input-required 却没有自己的 status：上一句「正在读…」不是它问的问题。
    fake.get_state = "input-required"
    fake.get_status = ""
    session = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    assert session["state"] == "waiting"
    assert [m["text"] for m in session["messages"]] == ["Build the site"]


def test_a_finished_turn_does_not_reuse_the_question_it_asked(
    client, app, fetcher
) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "input-required"
    fake.send_text = ""
    fake.send_status = "which city?"
    fake.install(app)
    session = open_session(client, agent["id"], "Plan a trip")
    assert session["messages"][-1]["text"] == "which city?"

    fake.get_state = "completed"
    fake.get_status = ""
    fake.get_text = ""
    session = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # 完成时没有新的话：上一句提问不能摇身变成最终答复。
    assert session["state"] == "done"
    assert [m["text"] for m in session["messages"]] == ["Plan a trip", "which city?"]


def test_progress_is_cleared_when_the_turn_finishes(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.send_text = ""
    fake.send_status = "working on it"
    fake.install(app)
    session = open_session(client, agent["id"], "Long job")
    assert session["progress"]["text"] == "working on it"

    fake.get_state = "completed"
    fake.get_text = "the answer"
    done = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # 完成了还挂着「生成中」比没有这行更糟。
    assert done["state"] == "done"
    assert done["progress"] is None
    assert done["messages"][-1]["text"] == "the answer"


def test_cancel_stops_the_turn(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.install(app)
    session = open_session(client, agent["id"], "Long job")

    canceled = client.post(f"/api/panel/sessions/{session['id']}/cancel").json()["session"]

    assert canceled["state"] == "canceled"
    assert canceled["messages"][-1]["text"] == "stopped"


# --------------------------------------------------------------- the callback


def test_a_push_callback_completes_the_conversation(client, app, fetcher) -> None:
    agent = register(
        client,
        fetcher,
        url=PUSH_CARD_URL,
        capabilities={"streaming": False, "pushNotifications": True},
    )
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.install(app)
    session = open_session(client, agent["id"], "Where is Lisbon?")

    with app.state.db.connect() as connection:
        row = connection.execute(
            "SELECT push_token FROM tasks WHERE session_id = ?", (session["id"],)
        ).fetchone()
    assert row is not None and row["push_token"], "the task must carry a callback token"

    response = client.post(
        f"/a2a/push/{row['push_token']}",
        json=task_payload("task-1", "completed", "answered by the callback"),
    )
    assert response.status_code == 200

    updated = client.get(f"/api/panel/sessions/{session['id']}").json()["session"]
    assert updated["state"] == "done"
    assert updated["messages"][-1]["text"] == "answered by the callback"


def test_a_callback_for_an_older_turn_is_left_alone(client, app, fetcher) -> None:
    """The conversation follows its *current* turn; stale callbacks change nothing."""
    agent = register(
        client,
        fetcher,
        url=PUSH_CARD_URL,
        capabilities={"streaming": False, "pushNotifications": True},
    )
    fake = ScriptedAgent()
    fake.send_state = "working"
    fake.install(app)
    session = open_session(client, agent["id"], "first")
    client.post(f"/api/panel/sessions/{session['id']}/cancel")

    # Move on to a fresh turn, then deliver a late callback for the old task.
    fake.send_state = "working"
    client.post(f"/api/panel/sessions/{session['id']}/messages", json={"text": "second"})

    with app.state.db.connect() as connection:
        row = connection.execute(
            "SELECT push_token FROM tasks WHERE session_id = ? AND id != ?",
            (session["id"], session["id"]),
        ).fetchone()

    before = client.get(f"/api/panel/sessions/{session['id']}").json()["session"]
    if row is not None and row["push_token"]:
        client.post(
            f"/a2a/push/{row['push_token']}",
            json=task_payload("task-1", "completed", "late answer for turn one"),
        )
    after = client.get(f"/api/panel/sessions/{session['id']}").json()["session"]
    assert [m["text"] for m in after["messages"]] == [m["text"] for m in before["messages"]]


# ------------------------------------------------------------- housekeeping


def test_rename_and_delete_a_conversation(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    ScriptedAgent().install(app)
    session = open_session(client, agent["id"], "Where is Lisbon?")

    renamed = client.patch(
        f"/api/panel/sessions/{session['id']}", json={"title": "旅行计划"}
    ).json()["session"]
    assert renamed["title"] == "旅行计划"

    assert client.delete(f"/api/panel/sessions/{session['id']}").status_code == 204
    assert client.get(f"/api/panel/sessions/{session['id']}").status_code == 404
    # The protocol ledger is not the conversation's to delete.
    assert client.get("/api/panel/tasks").json()["items"], "the task row must survive"


def test_the_agent_list_counts_conversations(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    ScriptedAgent().install(app)
    open_session(client, agent["id"], "hello")

    items = client.get("/api/panel/agents").json()["items"]
    assert items[0]["sessionCount"] == 1
    assert items[0]["lastSessionAt"]

    summary = client.get("/api/panel/summary").json()
    assert summary["sessions"] == {"total": 1, "active": 0}


def test_a_conversation_payload_speaks_about_messages_only(client, app, fetcher) -> None:
    """The whole point of the layer: no protocol vocabulary reaches the UI."""
    agent = register(client, fetcher)
    ScriptedAgent().install(app)
    session = open_session(client, agent["id"], "hello")

    assert not [key for key in session if "task" in key.lower() or "context" in key.lower()]
    for message in session["messages"]:
        # ``attachments`` is the panel's own word for the pictures a message
        # carries; it is still not protocol vocabulary, which is what this guards.
        assert set(message) == {"id", "role", "text", "at", "state", "attachments"}


def test_unknown_ids_are_404(client) -> None:
    assert (
        client.post(
            "/api/panel/agents/a_0000000000000000/sessions", json={"text": "hi"}
        ).status_code
        == 404
    )
    assert client.get("/api/panel/sessions/s_unknown").status_code == 404
    assert (
        client.post("/api/panel/sessions/s_unknown/messages", json={"text": "hi"}).status_code
        == 404
    )


def test_sending_to_a_removed_agent_is_refused_clearly(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    ScriptedAgent().install(app)
    session = open_session(client, agent["id"], "hello")

    with app.state.db.connect() as connection:
        connection.execute("DELETE FROM agents WHERE id = ?", (agent["id"],))

    response = client.post(
        f"/api/panel/sessions/{session['id']}/messages", json={"text": "still there?"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "session_agent_gone"


def test_an_empty_message_is_refused(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    ScriptedAgent().install(app)
    response = client.post(f"/api/panel/agents/{agent['id']}/sessions", json={"text": "   "})
    assert response.status_code == 201
    empty = response.json()["session"]
    assert empty["state"] == "idle"
    assert empty["messages"] == []

    refused = client.patch(f"/api/panel/sessions/{empty['id']}", json={"title": "  "})
    assert refused.status_code == 400
    assert refused.json()["code"] == "bad_title"


def test_sessions_are_listed_newest_activity_first(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    ScriptedAgent().install(app)
    first = open_session(client, agent["id"], "first")
    second = open_session(client, agent["id"], "second")

    items = client.get("/api/panel/sessions", params={"agentId": agent["id"]}).json()["items"]
    assert [item["id"] for item in items] == [second["id"], first["id"]]
    assert items[0]["lastText"] == "done"
    assert items[0]["title"] == "second"

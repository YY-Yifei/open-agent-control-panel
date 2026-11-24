"""A task the agent no longer has (``TaskNotFoundError``) must end, not hang.

The report that produced this file: a remote A2A service restarted with an
in-memory task table, so from 15:30 on every ``tasks/get`` answered ``-32001``
while the panel kept saying ``working`` — the progress line frozen at "已运行
45m54s", the composer disabled with "对方正在处理，请稍候…", and the stop button
failing with the same ``-32001``. Reloading the page was the only way out.

The distinction these tests pin down is *permanent* versus *transient*: a task
the agent has forgotten can never report again, so waiting is not an option; a
timeout or a 5xx can, so the turn must survive it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from conftest import make_card

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"

#: What a conformant agent answers when the task id is not in its table.
GONE = -32001
#: The words the reporting deployment actually got back. Quoted verbatim: the
#: panel must keep the agent's own words, in the agent's own language.
GONE_TEXT = "没有这个任务：task-1（进程重启后任务表会清空）"


def task_payload(task_id: str, state: str, status_text: str = "") -> dict[str, Any]:
    status: dict[str, Any] = {"state": state}
    if status_text:
        status["message"] = {"role": "agent", "parts": [{"kind": "text", "text": status_text}]}
    return {
        "kind": "task",
        "id": task_id,
        "contextId": "ctx-1",
        "status": status,
        "history": [],
    }


class RestartingAgent:
    """An agent whose task table lives in memory — and can be emptied mid-turn.

    ``forget()`` is the restart: the rows are gone, so every id-addressed call
    afterwards is a ``-32001``. ``unreachable`` models the *other* kind of
    failure, which must not end the turn.
    """

    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.unreachable = False
        self._turns = 0

    def install(self, app) -> "RestartingAgent":
        app.state.a2a.transport = httpx.MockTransport(self._handle)
        return self

    def forget(self) -> None:
        """Lose every task, the way a process restart does."""
        self.tasks.clear()

    def resume(self, task_id: str, state: str = "completed", text: str = "the answer") -> None:
        payload = task_payload(task_id, state)
        if text:
            payload["artifacts"] = [{"parts": [{"kind": "text", "text": text}]}]
        self.tasks[task_id] = payload

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.recorded.append(body)
        method = body["method"]

        def ok(result: Any) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        def gone() -> httpx.Response:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": GONE, "message": GONE_TEXT}},
            )

        if self.unreachable:
            return httpx.Response(503, json={"error": "the agent is restarting"})

        if method in {"message/send", "SendMessage"}:
            message = body["params"].get("message") or {}
            task_id = message.get("taskId")
            if task_id is not None and task_id not in self.tasks:
                return gone()
            if task_id is None:
                self._turns += 1
                task_id = f"task-{self._turns}"
            self.tasks[task_id] = task_payload(task_id, "working", "第 48/200 步 · 调用 run_shell")
            return ok(self.tasks[task_id])

        if method in {"tasks/get", "GetTask"}:
            task_id = body["params"]["id"]
            if task_id not in self.tasks:
                return gone()
            return ok(self.tasks[task_id])

        if method in {"tasks/cancel", "CancelTask"}:
            task_id = body["params"]["id"]
            if task_id not in self.tasks:
                return gone()
            self.tasks[task_id] = task_payload(task_id, "canceled", "stopped")
            return ok(self.tasks[task_id])

        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "unknown"}},
        )


# ----------------------------------------------------------------------- helpers


def register(client, fetcher) -> dict:
    fetcher.cards[CARD_URL] = make_card()
    return client.post("/v1/agents", json={"url": CARD_URL}).json()


def open_session(client, agent_id: str, text: str = "keep going") -> dict:
    response = client.post(f"/api/panel/agents/{agent_id}/sessions", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()["session"]


def task_of(app, session: dict) -> dict:
    """The ledger row behind a conversation's open turn."""
    with app.state.db.connect() as connection:
        session_row = connection.execute(
            "SELECT task_id FROM sessions WHERE id = ?", (session["id"],)
        ).fetchone()
        assert session_row is not None and session_row["task_id"]
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (session_row["task_id"],)
        ).fetchone()
    assert row is not None
    return dict(row)


# ------------------------------------------------------------------------- the bug


def test_a_lost_task_ends_the_turn_instead_of_staying_working(client, app, fetcher) -> None:
    """The reported case: poll after a restart must unlock, not hang for ever."""
    agent = register(client, fetcher)
    bot = RestartingAgent().install(app)
    session = open_session(client, agent["id"])

    assert session["state"] == "working"
    assert session["progress"]["text"].startswith("第 48/200 步")

    bot.forget()
    after = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # ``failed`` is what the UI reads: it unlocks the composer and stops the poll.
    # Leaving it on ``working`` is the bug — nothing would ever move it again.
    assert after["state"] == "failed"
    # A progress line that can never report again is worse than none.
    assert after["progress"] is None

    note = after["messages"][-1]
    assert note["role"] == "error"
    # The sentence a person reads is the panel's, so it travels as a *code* and is
    # written in whatever language the UI is showing...
    assert note["code"] == "msg.task_gone"
    # ...while ``text`` keeps the agent's own words, untranslated and unparaphrased.
    assert GONE_TEXT in note["text"]
    assert str(GONE) in note["text"]
    assert "远端" not in note["text"]

    row = task_of(app, after)
    assert row["state"] == "interrupted"
    assert row["progress_text"] is None
    assert GONE_TEXT in row["error"]

    detail = client.get(f"/api/panel/tasks/{row['id']}").json()["task"]
    assert "interrupted" in [event["kind"] for event in detail["events"]]


def test_stop_ends_the_turn_locally_when_the_agent_forgot_it(client, app, fetcher) -> None:
    """Cancel must never depend on the remote agreeing: it is the operator's exit."""
    agent = register(client, fetcher)
    bot = RestartingAgent().install(app)
    session = open_session(client, agent["id"])
    bot.forget()

    response = client.post(f"/api/panel/sessions/{session['id']}/cancel")
    assert response.status_code == 200, response.text
    after = response.json()["session"]

    assert after["state"] == "canceled"
    assert after["progress"] is None
    note = after["messages"][-1]
    assert note["code"] == "msg.cancelled_locally"
    assert GONE_TEXT in note["text"]
    assert task_of(app, after)["state"] == "canceled"


def test_a_message_into_a_lost_task_ends_the_turn(client, app, fetcher) -> None:
    """A follow-up is addressed to a task id the agent no longer has.

    It never landed, so the turn is over — and it must not silently start a
    second turn behind the operator's back (that would answer one question
    twice).
    """
    agent = register(client, fetcher)
    bot = RestartingAgent().install(app)
    session = open_session(client, agent["id"], "first")
    turns_before = bot._turns

    bot.forget()
    after = client.post(
        f"/api/panel/sessions/{session['id']}/messages", json={"text": "and then?"}
    ).json()["session"]

    assert after["state"] == "failed"
    assert after["messages"][-1]["code"] == "msg.task_gone"
    # The line the operator typed is still in the transcript; it just has no answer.
    assert any(message["text"] == "and then?" for message in after["messages"])
    assert bot._turns == turns_before


def test_a_transient_failure_leaves_the_turn_running(client, app, fetcher) -> None:
    """The guard for the other half: a blip must not end a turn that still runs."""
    agent = register(client, fetcher)
    bot = RestartingAgent().install(app)
    session = open_session(client, agent["id"])
    task_id = task_of(app, session)["remote_task_id"]

    bot.unreachable = True
    after = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    assert after["state"] == "working"
    assert task_of(app, after)["state"] == "working"

    # And when the agent comes back, the same turn finishes normally.
    bot.unreachable = False
    bot.resume(task_id, "completed", "the answer")
    done = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    assert done["state"] == "done"
    assert done["messages"][-1]["role"] == "agent"
    assert done["messages"][-1]["text"] == "the answer"


def test_a_late_callback_cannot_revive_an_interrupted_task() -> None:
    """``interrupted`` is a one-way door, like every other terminal state.

    Push deliveries are retried and arrive out of order; a ``working`` callback
    that lands after the panel has declared the task lost must not drag the turn
    back to ``working`` and re-hang the UI.
    """
    from oacp.tasks import _outranks

    assert not _outranks("working", "interrupted")
    assert not _outranks("submitted", "interrupted")
    # ...and the reverse direction: an agent that reports *our* word is not telling us
    # anything, so it may not overwrite a verdict either.
    assert not _outranks("interrupted", "completed")


def test_a_finished_task_keeps_its_verdict_when_the_agent_forgets_it(client, app, fetcher) -> None:
    """A refresh may not turn ``completed`` into ``interrupted``.

    The bug is about a turn that is still *running*. A task that already finished
    keeps what happened: rewriting it would erase the fact that the answer arrived,
    and a conversation would print "the agent no longer has this task" right under a
    complete answer.
    """
    agent = register(client, fetcher)
    bot = RestartingAgent().install(app)
    session = open_session(client, agent["id"])
    remote_id = task_of(app, session)["remote_task_id"]

    bot.resume(remote_id, "completed", "the answer")
    done = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    assert done["state"] == "done"

    bot.forget()
    refreshed = client.post(f"/api/panel/tasks/{task_of(app, done)['id']}/refresh").json()["task"]
    assert refreshed["state"] == "completed"
    assert refreshed["text"] == "the answer"
    # Worth a line in the audit trail — the id is dead at the agent — but the
    # verdict stands.
    assert [event["kind"] for event in refreshed["events"]][-1] == "gone-after-end"

    after = client.get(f"/api/panel/sessions/{session['id']}").json()["session"]
    assert after["state"] == "done"
    assert after["messages"][-1]["role"] == "agent"
    assert after["messages"][-1]["text"] == "the answer"


def test_refreshing_an_interrupted_task_changes_nothing(client, app, fetcher) -> None:
    """A settled verdict is settled: the second lookup is a no-op, not a rewrite."""
    agent = register(client, fetcher)
    bot = RestartingAgent().install(app)
    session = open_session(client, agent["id"])
    bot.forget()

    interrupted = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    assert interrupted["state"] == "failed"
    row = task_of(app, interrupted)
    assert row["state"] == "interrupted"

    again = client.post(f"/api/panel/tasks/{row['id']}/refresh").json()["task"]
    assert again["state"] == "interrupted"
    assert again["error"] == row["error"]


def test_a_task_not_found_is_never_retried_in_the_other_dialect(client, app, fetcher) -> None:
    """A missing task is the same answer in both dialects: retrying loses the verdict.

    Some deployments answer a JSON-RPC error *with* a 4xx status, and 404 is in the
    client's retryable set for dialect reasons. Retrying a ``-32001`` asked the agent
    a second question (``tasks/get`` then ``GetTask``), got "method not found", and
    the verdict vanished — the turn went back to sitting on ``working``, which is the
    very failure this file exists to pin down.
    """
    agent = register(client, fetcher)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body["method"])
        if body["method"] in {"message/send", "SendMessage"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task_payload("task-1", "working", "still running"),
                },
            )
        return httpx.Response(
            400,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": GONE, "message": GONE_TEXT}},
        )

    app.state.a2a.transport = httpx.MockTransport(handler)
    session = open_session(client, agent["id"])
    after = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    assert calls[-1] == "tasks/get"
    assert "GetTask" not in calls
    assert after["state"] == "failed"


def test_a_reply_keeps_its_verdict_when_the_agent_forgets_the_task(client, app, fetcher) -> None:
    """``replied`` is a finished answer too — the ledger invented that word itself.

    A follow-up answered with a bare ``Message`` instead of a Task leaves a row in
    ``replied`` that still carries a remote task id, so it stays refreshable. A later
    ``-32001`` must not rewrite it: the answer is already in the conversation.
    """
    agent = register(client, fetcher)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body["method"])
        if body["method"] not in {"message/send", "SendMessage", "tasks/get", "GetTask"}:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}},
            )
        message = body["params"].get("message") or {}
        if body["method"] in {"message/send", "SendMessage"} and not message.get("taskId"):
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task_payload("task-1", "working", "still running"),
                },
            )
        if body["method"] in {"message/send", "SendMessage"}:
            # Answered with a bare Message: no task at all for this turn.
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "kind": "message",
                        "messageId": "m-1",
                        "role": "agent",
                        "parts": [{"kind": "text", "text": "answered directly"}],
                    },
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": GONE, "message": GONE_TEXT}},
        )

    app.state.a2a.transport = httpx.MockTransport(handler)
    session = open_session(client, agent["id"], "first")
    after = client.post(
        f"/api/panel/sessions/{session['id']}/messages", json={"text": "and then?"}
    ).json()["session"]
    assert after["state"] == "done"

    row = task_of(app, after)
    assert row["state"] == "replied"

    refreshed = client.post(f"/api/panel/tasks/{row['id']}/refresh").json()["task"]
    assert refreshed["state"] == "replied"
    assert refreshed["error"] is None


def test_a_bare_404_is_not_retried_either(client, app, fetcher) -> None:
    """The second shape of "it is not here": a 404 with a body and no ``error``.

    Retrying it under the other method name replaced the verdict with "method not
    found", so the turn stayed on ``working`` — the documented rule has to hold at
    both places a failure can leave the client.
    """
    agent = register(client, fetcher)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body["method"])
        if body["method"] in {"message/send", "SendMessage"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task_payload("task-1", "working", "still running"),
                },
            )
        return httpx.Response(404, json={"detail": "no such task"})

    app.state.a2a.transport = httpx.MockTransport(handler)
    session = open_session(client, agent["id"])
    after = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    assert calls[-1] == "tasks/get"
    assert "GetTask" not in calls
    assert after["state"] == "failed"


def test_a_bare_404_on_a_first_send_still_falls_back_to_the_other_dialect(
    client, app, fetcher
) -> None:
    """Only *task-scoped* calls read 404 as "the task is gone".

    On the first `SendMessage` a 404 is a method/path mismatch — the reason 404 is in
    the client's retryable set at all — so the 0.3 -> 1.x fallback has to stay alive.
    Making it permanent would turn a recoverable deployment mistake into a hard
    failure, which is the regression this test guards.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body["method"])
        if body["method"] == "message/send":
            # The 0.3 spelling is not served here; the 1.x one is.
            return httpx.Response(404, json={"detail": "no such method"})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": task_payload("task-9", "completed"),
            },
        )

    app.state.a2a.transport = httpx.MockTransport(handler)
    agent = register(client, fetcher)
    task = client.post(
        f"/api/panel/agents/{agent['id']}/tasks", json={"text": "hello"}
    ).json()["task"]

    assert calls == ["message/send", "SendMessage"]
    assert task["state"] == "completed"
    assert task["remoteTaskId"] == "task-9"


def test_a_gateway_wrapped_404_on_a_task_scoped_call_is_still_gone(client, app, fetcher) -> None:
    """The decision is made once, where the action is still known.

    A proxy in front of the agent can answer 404 with a JSON-RPC error of its own
    making (``-32600`` here). On ``get``/``cancel`` that is indistinguishable from a
    lost task, so it must end the turn — not be retried under the other dialect
    (which answers "method not found" and hides the verdict), and not be re-derived
    into "not gone" by a function that no longer knows which action it was.
    """
    agent = register(client, fetcher)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body["method"])
        if body["method"] in {"message/send", "SendMessage"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task_payload("task-1", "working", "still running"),
                },
            )
        return httpx.Response(
            404,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32600, "message": "bad gateway"}},
        )

    app.state.a2a.transport = httpx.MockTransport(handler)
    session = open_session(client, agent["id"])
    after = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    assert calls[-1] == "tasks/get"
    assert "GetTask" not in calls
    assert after["state"] == "failed"


def test_a_string_dialect_code_on_a_task_scoped_404_still_falls_back(client, app, fetcher) -> None:
    """``-32601`` says "no such method" — as a string it must still say that.

    The client accepts numeric and string codes, and reading them differently in the
    two places that classify an error is how a string ``-32601`` became "the task is
    gone" and ended a turn that was merely being spoken to in the wrong dialect.
    """
    agent = register(client, fetcher)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body["method"])
        if body["method"] in {"message/send", "SendMessage"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task_payload("task-1", "working", "still running"),
                },
            )
        return httpx.Response(
            404,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": "-32601", "message": "method not found"},
            },
        )

    app.state.a2a.transport = httpx.MockTransport(handler)
    session = open_session(client, agent["id"])
    after = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]

    # Both spellings were tried, and the turn was not declared over.
    assert calls[-2:] == ["tasks/get", "GetTask"]
    assert after["state"] == "working"


def test_a_failed_refresh_does_not_stamp_an_error_on_a_finished_turn(client, app, fetcher) -> None:
    """A finished turn keeps its verdict *and* its empty error field.

    Otherwise a 500 during a refresh of a canceled task writes ``error`` onto
    ``canceled``, and the conversation prints "the agent no longer has this task, so it
    was stopped locally" under a reason that has nothing to do with it.
    """
    agent = register(client, fetcher)
    bot = RestartingAgent().install(app)
    session = open_session(client, agent["id"], "hello")

    bot.resume(task_of(app, session)["remote_task_id"], "canceled", "stopped")
    done = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    assert done["state"] == "canceled"
    task_id = task_of(app, done)["id"]

    bot.unreachable = True
    refreshed = client.post(f"/api/panel/tasks/{task_id}/refresh").json()["task"]
    assert refreshed["state"] == "canceled"
    assert refreshed["error"] is None
    assert [event["kind"] for event in refreshed["events"]][-1] == "failed-after-end"

    after = client.get(f"/api/panel/sessions/{session['id']}").json()["session"]
    assert [m for m in after["messages"] if m["role"] == "error"] == []

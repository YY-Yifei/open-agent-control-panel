"""Local ledger of tasks that the panel submitted to agents.

A2A keeps the authoritative task state on the agent side; the panel keeps a
snapshot so the UI can show history, errors and follow-ups without replaying
every request. Snapshots are refreshed on demand and on every follow-up.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any

from .adp import card as cardmod
from .adp.service import RegistryService
from .a2a.client import A2AClient, A2AError
from .db import Database
from .util import iso, json_dumps, json_loads, new_id, title_from, truncate

LOCAL_ERROR_STATE = "error"
LOCAL_REPLY_STATE = "replied"
PAGE_LIMIT = 50


class TaskNotFound(Exception):
    """The requested local task does not exist."""


class TaskService:
    """Submit, follow and cancel tasks through the A2A client."""

    def __init__(self, db: Database, registry: RegistryService, client: A2AClient) -> None:
        self.db = db
        self.registry = registry
        self.client = client

    # ------------------------------------------------------------------ writing

    def submit(
        self,
        agent_id: str,
        text: str,
        *,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a message to an agent and record the resulting task."""
        agent = self.registry.get(agent_id)
        if agent is None:
            raise LookupError(f"no agent with id {agent_id}")

        card = agent["card"]
        endpoint = cardmod.pick_endpoint(card) or {}
        task_id = new_id("t")
        now = iso()
        request = {"text": text, "contextId": context_id, "metadata": metadata}

        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, agent_id, agent_name, agent_url, endpoint, state, title,
                    input_text, request_json, created_at, updated_at, message_count
                ) VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, 1)
                """,
                (
                    task_id,
                    agent_id,
                    str(card.get("name") or agent["url"]),
                    agent["url"],
                    endpoint.get("url") or "",
                    title_from(text),
                    text,
                    json_dumps(request),
                    now,
                    now,
                ),
            )
            self._event(connection, task_id, now, "submit", {"text": truncate(text, 500)})

        try:
            call = self.client.send_message(card, text, context_id=context_id, metadata=metadata)
        except A2AError as exc:
            return self._record_error(task_id, exc)

        return self._apply_result(task_id, call, kind="response")

    def follow_up(
        self,
        task_id: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send another user message into the same task/context."""
        task = self._row(task_id)
        agent = self.registry.get(task["agent_id"])
        if agent is None:
            raise LookupError(f"the agent for task {task_id} is no longer registered")

        context_id = task["context_id"] or None
        remote_id = task["remote_task_id"] or None
        try:
            call = self.client.send_message(
                agent["card"],
                text,
                context_id=context_id,
                task_id=remote_id,
                metadata=metadata,
            )
        except A2AError as exc:
            return self._record_error(task_id, exc, increment_messages=True)

        return self._apply_result(task_id, call, kind="message", increment_messages=True)

    def refresh(self, task_id: str) -> dict[str, Any]:
        """Re-read the task state from the agent (``GetTask``)."""
        task = self._row(task_id)
        if not task["remote_task_id"]:
            raise ValueError("this task has no remote task id to refresh")
        agent = self.registry.get(task["agent_id"])
        if agent is None:
            raise LookupError(f"the agent for task {task_id} is no longer registered")

        try:
            call = self.client.get_task(agent["card"], task["remote_task_id"], history_length=50)
        except A2AError as exc:
            return self._record_error(task_id, exc, state=task["state"])

        return self._apply_result(task_id, call, kind="refresh")

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Ask the agent to cancel the task (``CancelTask``)."""
        task = self._row(task_id)
        if not task["remote_task_id"]:
            raise ValueError("this task has no remote task id to cancel")
        agent = self.registry.get(task["agent_id"])
        if agent is None:
            raise LookupError(f"the agent for task {task_id} is no longer registered")

        try:
            call = self.client.cancel_task(agent["card"], task["remote_task_id"])
        except A2AError as exc:
            return self._record_error(task_id, exc, state=task["state"])

        return self._apply_result(task_id, call, kind="cancel")

    def delete(self, task_id: str) -> None:
        """Remove a task from the local ledger (the agent keeps its own copy)."""
        with self.db.connect() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            if cursor.rowcount == 0:
                raise TaskNotFound(task_id)
            connection.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))

    # ------------------------------------------------------------------ reading

    def get(self, task_id: str) -> dict[str, Any]:
        """One task with its event log and the raw request/response payloads."""
        try:
            row = self._row(task_id)
        except TaskNotFound:
            raise
        return self._view(row, detail=True)

    def list_tasks(
        self,
        *,
        agent_id: str | None = None,
        state: str | None = None,
        q: str | None = None,
        limit: int = PAGE_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search the ledger, newest first."""
        limit = max(1, min(int(limit), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if state == "active":
            clauses.append("state IN ('submitted', 'working', 'input-required', 'auth-required')")
        elif state:
            clauses.append("state = ?")
            params.append(state)
        if q:
            clauses.append("(lower(title) LIKE ? OR lower(input_text) LIKE ? OR remote_task_id LIKE ?)")
            needle = f"%{q.casefold()}%"
            params.extend([needle, needle, f"%{q}%"])

        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC"

        with self.db.connect() as connection:
            rows = connection.execute(sql, params).fetchall()

        start = 0
        if cursor:
            start = _cursor_index(rows, cursor)

        page = rows[start : start + limit]
        next_cursor = None
        if start + limit < len(rows):
            last = page[-1]
            next_cursor = _encode_cursor(last["created_at"], last["id"])

        return {"items": [self._view(row) for row in page], "nextCursor": next_cursor}

    def stats(self) -> dict[str, Any]:
        """Counts used by the dashboard header."""
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
            ).fetchall()
        by_state = {row["state"]: row["n"] for row in rows}
        active = sum(
            count for state, count in by_state.items() if state in cardmod.ACTIVE_STATES
        )
        return {"total": sum(by_state.values()), "active": active, "byState": by_state}

    def agent_counts(self, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Per-agent task counts, in one query."""
        if not agent_ids:
            return {}
        placeholders = ",".join("?" for _ in agent_ids)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT agent_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN state IN ('submitted', 'working', 'input-required', 'auth-required')
                                THEN 1 ELSE 0 END) AS active,
                       MAX(updated_at) AS last_at
                  FROM tasks
                 WHERE agent_id IN ({placeholders})
                 GROUP BY agent_id
                """,
                agent_ids,
            ).fetchall()
        return {
            row["agent_id"]: {
                "taskCount": int(row["total"] or 0),
                "activeTaskCount": int(row["active"] or 0),
                "lastTaskAt": row["last_at"],
            }
            for row in rows
        }

    # ------------------------------------------------------------------ internals

    def _row(self, task_id: str) -> sqlite3.Row:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return row

    def _apply_result(self, task_id: str, call: Any, *, kind: str, increment_messages: bool = False) -> dict[str, Any]:
        view = cardmod.interpret_result(call.result)
        now = iso()
        state = view["state"]
        if view["kind"] == "message":
            state = LOCAL_REPLY_STATE

        row = self._row(task_id)
        message_count = int(row["message_count"]) + (1 if increment_messages else 0)
        remote_task_id = view.get("taskId") or row["remote_task_id"]
        context_id = view.get("contextId") or row["context_id"]

        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                   SET remote_task_id = ?, context_id = ?, state = ?, response_json = ?,
                       endpoint = ?, error = NULL, updated_at = ?, synced_at = ?,
                       message_count = ?
                 WHERE id = ?
                """,
                (
                    remote_task_id,
                    context_id,
                    state,
                    json_dumps(view["raw"]),
                    call.endpoint,
                    now,
                    now,
                    message_count,
                    task_id,
                ),
            )
            self._event(
                connection,
                task_id,
                now,
                kind,
                {"state": state, "text": truncate(view.get("text") or view.get("statusText"), 500)},
            )
        return self.get(task_id)

    def _record_error(
        self,
        task_id: str,
        exc: A2AError,
        *,
        state: str | None = None,
        increment_messages: bool = False,
    ) -> dict[str, Any]:
        now = iso()
        message = exc.message
        row = self._row(task_id)
        message_count = int(row["message_count"]) + (1 if increment_messages else 0)
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                   SET state = ?, error = ?, updated_at = ?, message_count = ?
                 WHERE id = ?
                """,
                (state or LOCAL_ERROR_STATE, message, now, message_count, task_id),
            )
            self._event(connection, task_id, now, "error", {"error": message})
        return self.get(task_id)

    def _event(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        at: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO task_events (task_id, at, kind, payload) VALUES (?, ?, ?, ?)",
            (task_id, at, kind, json_dumps(payload)),
        )

    def _view(self, row: sqlite3.Row, *, detail: bool = False) -> dict[str, Any]:
        response = json_loads(row["response_json"], None)
        view: dict[str, Any] = {
            "id": row["id"],
            "agentId": row["agent_id"],
            "agentName": row["agent_name"],
            "agentUrl": row["agent_url"],
            "endpoint": row["endpoint"],
            "remoteTaskId": row["remote_task_id"],
            "contextId": row["context_id"],
            "state": row["state"],
            "title": row["title"],
            "inputText": row["input_text"],
            "error": row["error"],
            "messageCount": int(row["message_count"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "syncedAt": row["synced_at"],
            "text": _latest_text(response),
            "artifactCount": _artifact_count(response),
        }
        if detail:
            view["request"] = json_loads(row["request_json"], {})
            view["response"] = response
            with self.db.connect() as connection:
                events = connection.execute(
                    "SELECT at, kind, payload FROM task_events WHERE task_id = ? ORDER BY id ASC",
                    (row["id"],),
                ).fetchall()
            view["events"] = [
                {"at": event["at"], "kind": event["kind"], "payload": json_loads(event["payload"], {})}
                for event in events
            ]
        return view


def _latest_text(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    view = cardmod.interpret_result(response)
    return view.get("text") or view.get("statusText") or ""


def _artifact_count(response: Any) -> int:
    if not isinstance(response, dict):
        return 0
    artifacts = response.get("artifacts")
    return len(artifacts) if isinstance(artifacts, list) else 0


# ------------------------------------------------------------------ cursors


def _encode_cursor(created_at: str, task_id: str) -> str:
    payload = json.dumps({"c": created_at, "i": task_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_index(rows: list[sqlite3.Row], cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        created_at, task_id = str(data["c"]), str(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("cursor is not valid") from exc

    for index, row in enumerate(rows):
        if row["created_at"] == created_at and row["id"] == task_id:
            return index + 1
    raise ValueError("cursor no longer matches any entry")

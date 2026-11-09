"""Local ledger of tasks that the panel submitted to agents.

A2A keeps the authoritative task state on the agent side; the panel keeps a
snapshot so the UI can show history, errors and follow-ups without replaying
every request. Snapshots are refreshed on demand, on every follow-up, and — when
push notifications are switched on — whenever the agent calls back.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any

from .adp import card as cardmod
from .adp.service import RegistryService
from .a2a.client import A2AClient, A2AError
from .config import Settings
from .db import Database
from .errors import PanelError
from .util import iso, json_dumps, json_loads, new_id, new_token, title_from, truncate

LOCAL_ERROR_STATE = "error"
LOCAL_REPLY_STATE = "replied"
PAGE_LIMIT = 50

#: Where each non-terminal state sits on the one-way ladder a task climbs. Used
#: only by the push guard: a callback that arrives late must not drag the ledger
#: backwards. Terminal states are handled before this table is consulted, so a
#: terminal state always wins and can never be replaced by a running one.
_PROGRESS: dict[str, int] = {
    "unknown": 0,
    LOCAL_ERROR_STATE: 1,
    "submitted": 2,
    "working": 3,
    LOCAL_REPLY_STATE: 4,
    "input-required": 4,
    "auth-required": 4,
}

#: ``push_state`` values. ``offered`` is transient (token minted, callback not
#: confirmed yet); ``settled`` means the task was already finished when we asked,
#: so there was nothing left to follow.
PUSH_OFFERED = "offered"
PUSH_REGISTERED = "registered"
PUSH_INLINE = "inline"
PUSH_SETTLED = "settled"
PUSH_UNSUPPORTED = "unsupported"


class TaskNotFound(PanelError):
    """The requested local task does not exist."""

    status = 404

    def __init__(self, task_id: str) -> None:
        super().__init__("task_not_found", task_id=task_id)


class PushError(Exception):
    """A callback that cannot be placed; ``status`` is the answer to send back.

    Deliberately *not* a ``PanelError``: the panel's error catalog is localized
    prose for humans in the UI, while this travels to another agent. A stable
    machine token and an HTTP status are the whole contract.
    """

    def __init__(self, reason: str, *, code: str, status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.status = status


class TaskService:
    """Submit, follow and cancel tasks through the A2A client."""

    def __init__(
        self,
        db: Database,
        registry: RegistryService,
        client: A2AClient,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.client = client
        self.settings = settings

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
            raise PanelError("agent_not_found", status=404, agent_id=agent_id)

        card = agent["card"]
        endpoint = cardmod.pick_endpoint(card) or {}
        task_id = new_id("t")
        now = iso()
        request = {"text": text, "contextId": context_id, "metadata": metadata}

        # The callback URL is minted *before* the send and stored with the row:
        # an agent that finishes (or calls back) while our RPC is still open must
        # already find its token, or the very first callback would be a 404.
        push_token, push_config, push_state = self._offer_push(card)

        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, agent_id, agent_name, agent_url, endpoint, state, title,
                    input_text, request_json, created_at, updated_at, message_count,
                    push_token, push_state
                ) VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, 1, ?, ?)
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
                    push_token,
                    push_state,
                ),
            )
            self._event(
                connection,
                task_id,
                now,
                "submit",
                {"text": truncate(text, 500), "push": push_state} if push_state else {"text": truncate(text, 500)},
            )

        try:
            call = self.client.send_message(
                card,
                text,
                context_id=context_id,
                metadata=metadata,
                push_config=push_config,
            )
        except A2AError as exc:
            return self._record_error(task_id, exc)

        result = self._apply_result(task_id, call, kind="response")
        if push_token is not None:
            # Registration happens after the snapshot was taken, so read the row
            # again: the caller must see "registered", not "offered".
            self._confirm_push(task_id, card, result, push_token)
            return self.get(task_id)
        return result

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
            raise PanelError("agent_gone", status=404, task_id=task_id)

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
            raise PanelError("task_not_refreshable", status=409)
        agent = self.registry.get(task["agent_id"])
        if agent is None:
            raise PanelError("agent_gone", status=404, task_id=task_id)

        try:
            call = self.client.get_task(agent["card"], task["remote_task_id"], history_length=50)
        except A2AError as exc:
            return self._record_error(task_id, exc, state=task["state"])

        return self._apply_result(task_id, call, kind="refresh")

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Ask the agent to cancel the task (``CancelTask``)."""
        task = self._row(task_id)
        if not task["remote_task_id"]:
            raise PanelError("task_not_cancelable", status=409)
        agent = self.registry.get(task["agent_id"])
        if agent is None:
            raise PanelError("agent_gone", status=404, task_id=task_id)

        try:
            call = self.client.cancel_task(agent["card"], task["remote_task_id"])
        except A2AError as exc:
            return self._record_error(task_id, exc, state=task["state"])

        return self._apply_result(task_id, call, kind="cancel")

    def apply_push(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Fold one agent callback into the ledger.

        A2A push notifications are best-effort: the agent may retry, and the
        deliveries may arrive out of order. So this is deliberately idempotent
        and monotonic — a repeat changes nothing, and a late ``working`` cannot
        overwrite a ``completed`` we already hold.

        Returns:
            ``{"status": "applied" | "duplicate" | "ignored", ...}`` for the
            caller to echo back.

        Raises:
            PushError: when the token is unknown or the payload is not our task.
        """
        row = self._row_by_token(token)
        view = cardmod.interpret_result(payload)
        if view["kind"] != "task":
            raise PushError("the callback payload is not an A2A Task", code="invalid_task")

        remote_id = view.get("taskId")
        if remote_id and row["remote_task_id"] and remote_id != row["remote_task_id"]:
            raise PushError(
                "the task id does not belong to this callback token",
                code="task_mismatch",
                status=409,
            )
        if not remote_id and not row["remote_task_id"]:
            raise PushError("the task id is missing", code="task_id_missing")

        current = str(row["state"] or "unknown")
        incoming = str(view["state"] or "unknown")
        previous = json_loads(row["response_json"], None)
        text = (view.get("text") or view.get("statusText") or "").strip()
        seen = (_latest_text(previous) or "").strip()

        if incoming == current and text == seen:
            verdict = "duplicate"
        elif _outranks(incoming, current):
            verdict = "applied"
        else:
            verdict = "ignored"

        now = iso()
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE tasks SET push_count = push_count + 1, last_push_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            if verdict == "applied":
                merged = _merge_task(previous, payload)
                connection.execute(
                    """
                    UPDATE tasks
                       SET state = ?, response_json = ?, remote_task_id = ?, context_id = ?,
                           synced_at = ?, updated_at = ?, error = NULL
                     WHERE id = ?
                    """,
                    (
                        incoming,
                        json_dumps(merged),
                        remote_id or row["remote_task_id"],
                        view.get("contextId") or row["context_id"],
                        now,
                        now,
                        row["id"],
                    ),
                )
            self._event(
                connection,
                row["id"],
                now,
                "push" if verdict == "applied" else f"push-{verdict}",
                {
                    "state": incoming,
                    "kept": current,
                    "text": truncate(text, 500),
                },
            )

        return {
            "status": verdict,
            "taskId": row["id"],
            "remoteTaskId": remote_id or row["remote_task_id"],
            "state": incoming if verdict == "applied" else current,
        }

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

    # ------------------------------------------------------------------ push

    def _offer_push(self, card: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str | None]:
        """Mint a callback token, or explain (as a stored state) why not.

        Three answers, in this order: push may be switched off globally; the card
        may not claim the capability; otherwise a fresh 192-bit token is minted
        and both halves of the config are returned.
        """
        settings = self.settings
        if settings is None or not settings.push_enabled:
            return None, None, None
        if not _supports_push(card):
            return None, None, PUSH_UNSUPPORTED
        token = new_token(24)
        config = {"url": f"{settings.push_callback_base}/{token}", "token": token}
        return token, config, PUSH_OFFERED

    def _confirm_push(
        self,
        task_id: str,
        card: dict[str, Any],
        view: dict[str, Any],
        token: str,
    ) -> None:
        """Settle the ``push_state`` once we know what the agent actually did.

        The config already travelled inline with the message, so the URL is known
        to a cooperative agent even if this follow-up call fails; ``set`` is what
        turns "offered" into a fact we can report.
        """
        state = str(view.get("state") or "")
        if state == LOCAL_REPLY_STATE or cardmod.is_terminal(state):
            # Nothing left to report: a callback could only repeat what we hold.
            self._mark_push(task_id, PUSH_SETTLED)
            return

        settings = self.settings
        remote_id = view.get("remoteTaskId")
        if not remote_id or settings is None:
            self._mark_push(task_id, PUSH_INLINE, error="the agent returned no task id")
            return

        config = {"url": f"{settings.push_callback_base}/{token}", "token": token}
        try:
            self.client.set_push_config(card, remote_id, config)
        except A2AError as exc:
            # The inline copy may still be honoured; either way the task is
            # running right now, so find out where it stands while we are here.
            self._mark_push(task_id, PUSH_INLINE, error=exc.message)
            self._catch_up(task_id, card, remote_id)
            return
        self._mark_push(task_id, PUSH_REGISTERED)

    def _catch_up(self, task_id: str, card: dict[str, Any], remote_id: str) -> None:
        """One ``GetTask`` after a failed registration, to close the race.

        A registration can fail *because* the task finished in between. Asking
        once turns that race into a normal snapshot instead of a task that sits
        on ``working`` forever with no callback coming.
        """
        try:
            call = self.client.get_task(card, remote_id, history_length=20)
        except A2AError as exc:
            now = iso()
            with self.db.connect() as connection:
                self._event(connection, task_id, now, "push-catchup-error", {"error": exc.message})
            return
        self._apply_result(task_id, call, kind="push-catchup")

    def _mark_push(self, task_id: str, state: str, *, error: str | None = None) -> None:
        now = iso()
        payload: dict[str, Any] = {"state": state}
        if error:
            payload["error"] = error
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                   SET push_state = ?, push_registered_at = COALESCE(?, push_registered_at)
                 WHERE id = ?
                """,
                (state, now if state in {PUSH_REGISTERED, PUSH_INLINE} else None, task_id),
            )
            self._event(connection, task_id, now, "push-register", payload)

    def _row_by_token(self, token: str) -> sqlite3.Row:
        """Look a task up by its callback token.

        The token *is* the credential, so the lookup is the check: it is 24 random
        bytes of ``token_urlsafe``, which makes guessing a live one infeasible
        long before timing side channels become the cheapest way in.
        """
        if not token or len(token) > 200:
            raise PushError("unknown callback token", code="unknown_callback_token", status=404)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE push_token = ?", (token,)
            ).fetchone()
        if row is None:
            raise PushError("unknown callback token", code="unknown_callback_token", status=404)
        return row

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
            "pushState": row["push_state"],
            "pushCount": int(row["push_count"] or 0),
            "lastPushAt": row["last_push_at"],
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


def _supports_push(card: dict[str, Any]) -> bool:
    """True when the card claims the push-notification capability.

    Capability-gated on purpose: offering a URL to an agent that never promised
    to call it back would just leave a task looking "followed" that is not.
    """
    capabilities = card.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("pushNotifications") is True


def _outranks(incoming: str, current: str) -> bool:
    """True when ``incoming`` may replace ``current``.

    A terminal state is a one-way door: once a task has completed there is
    nothing a non-terminal update could legitimately tell us, and out-of-order
    deliveries are the normal case for webhooks.
    """
    if cardmod.is_terminal(incoming):
        return True
    if cardmod.is_terminal(current):
        return False
    return _PROGRESS.get(incoming, 1) >= _PROGRESS.get(current, 1)


def _merge_task(previous: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep artefacts the callback omitted.

    A2A task objects are cumulative, so a conformant agent repeats every artifact
    it already produced. Some do not, and sending only a status update; letting
    that erase the answer the operator is reading would be a worse outcome than
    storing one slightly stale field.
    """
    merged = dict(payload)
    if not isinstance(previous, dict) or "status" not in previous:
        return merged
    if not merged.get("artifacts") and previous.get("artifacts"):
        merged["artifacts"] = previous["artifacts"]
    status = merged.get("status") if isinstance(merged.get("status"), dict) else {}
    old_status = previous.get("status") if isinstance(previous.get("status"), dict) else {}
    if not status.get("message") and old_status.get("message"):
        merged["status"] = dict(status, message=old_status["message"])
    return merged


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
        raise PanelError("bad_cursor", status=400) from exc

    for index, row in enumerate(rows):
        if row["created_at"] == created_at and row["id"] == task_id:
            return index + 1
    raise PanelError("cursor_mismatch", status=400)

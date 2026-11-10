"""Conversations — the panel's own model of talking to an agent.

A2A has no "conversation" object. It has a ``contextId`` that ties several tasks
to one thread, and a ``Task`` per exchange, and a ``Message`` inside that task.
Those are the right things for a protocol to have and the wrong things to show a
person, so this module keeps the conversation explicit: a session belongs to one
agent, holds an ordered list of messages, and owns exactly one *open* task at a
time.

The mapping, in one place so nothing else has to guess:

* ``sessions.context_id``  ↔ the A2A ``contextId`` the agent assigned;
* ``sessions.task_id``     ↔ the task carrying the current turn;
* a user message           → ``SendMessage`` (``follow_up`` while the task is
                             still open, a fresh ``submit`` once it is not);
* the agent's answer       → the task's text, folded into one message.

Everything that can change a task — the send's own result, a ``GetTask`` via
:meth:`SessionService.sync`, and a push callback via :meth:`absorb_task` — goes
through the same ``_absorb`` path, so a conversation cannot show two different
truths depending on which route the update arrived by.

The six words the panel exposes are ``idle``, ``working``, ``waiting``, ``done``,
``failed`` and ``canceled``. Protocol spellings (``input-required``,
``auth-required``, ``TASK_STATE_*``) are folded in here and never leave.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .adp import card as cardmod
from .adp.service import RegistryService
from .db import Database
from .errors import PanelError
from .tasks import TaskService
from .util import iso, new_id, title_from, truncate

IDLE = "idle"
WORKING = "working"
WAITING = "waiting"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"

#: A2A task states (plus the ledger's two local ones) folded onto the panel's
#: vocabulary. An unrecognised state becomes ``idle`` rather than an error: a
#: word we have not met is a display problem, not a reason to break a chat.
_STATE: dict[str, str] = {
    "submitted": WORKING,
    "working": WORKING,
    "input-required": WAITING,
    "auth-required": WAITING,
    "completed": DONE,
    "replied": DONE,  # a bare Message answer, see tasks.LOCAL_REPLY_STATE
    "canceled": CANCELED,
    "failed": FAILED,
    "rejected": FAILED,
    "error": FAILED,  # a transport failure recorded by the ledger
    "unknown": IDLE,
}

#: States in which the agent still owns the turn. Only these take a follow-up
#: into the same task; anything else starts a fresh task in the same context,
#: which is what gives a conversation its continuity.
_OPEN = {WORKING, WAITING}

PAGE_LIMIT = 100
MAX_TITLE = 80


def state_of(value: Any) -> str:
    """Fold any task-state spelling onto one of the panel's six words."""
    return _STATE.get(cardmod.normalize_state(value), IDLE)


class SessionNotFound(PanelError):
    """The requested conversation does not exist."""

    status = 404

    def __init__(self, session_id: str) -> None:
        super().__init__("session_not_found", session_id=session_id)


class SessionService:
    """Conversations, on top of the task ledger and the registry."""

    def __init__(
        self,
        db: Database,
        registry: RegistryService,
        tasks: TaskService,
    ) -> None:
        self.db = db
        self.registry = registry
        self.tasks = tasks

    # ------------------------------------------------------------------ writing

    def create(
        self,
        agent_id: str,
        *,
        text: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Open a conversation with one agent, optionally with a first message.

        Creating and sending in one call is the normal path: the UI never has to
        show an empty conversation that a user abandoned half a second later.
        """
        agent = self.registry.get(agent_id)
        if agent is None:
            raise PanelError("agent_not_found", status=404, agent_id=agent_id)

        session_id = new_id("s")
        now = iso()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, agent_id, agent_name, agent_url, title, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_id,
                    cardmod.card_summary(agent["card"])["name"] or agent["url"],
                    agent["url"],
                    _clean_title(title),
                    IDLE,
                    now,
                    now,
                ),
            )

        if text is not None and text.strip():
            return self.send(session_id, text)
        return self.get(session_id)

    def send(self, session_id: str, text: str) -> dict[str, Any]:
        """Send one user message and record whatever the agent answered.

        The agent call is synchronous, so this returns either the answer or the
        news that the turn is running; a caller that got ``working`` should stop
        asking and wait for the callback (or call :meth:`sync`).
        """
        body = (text or "").strip()
        if not body:
            raise PanelError("empty_message", status=400)

        row = self._row(session_id)
        if self.registry.get(row["agent_id"]) is None:
            raise PanelError("session_agent_gone", status=409, agent_id=row["agent_id"])

        self._remember(session_id, body)
        task = self._carry(row, body)
        self._bind(session_id, task)
        self._absorb(session_id, task)
        return self.get(session_id)

    def sync(self, session_id: str) -> dict[str, Any]:
        """Ask the agent whether the running turn has moved (``GetTask``)."""
        row = self._row(session_id)
        if row["state"] not in _OPEN or not row["task_id"]:
            return self.get(session_id)
        try:
            task = self.tasks.refresh(row["task_id"])
        except PanelError as exc:
            # Nothing to ask about: no remote id, or the row was pruned. The
            # conversation is still readable, so this is not the user's problem.
            if exc.code in {"task_not_refreshable", "task_not_found"}:
                return self.get(session_id)
            raise
        self._absorb(session_id, task)
        return self.get(session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        """Stop the running turn."""
        row = self._row(session_id)
        if not row["task_id"]:
            raise PanelError("nothing_to_cancel", status=409)
        try:
            task = self.tasks.cancel(row["task_id"])
        except PanelError as exc:
            if exc.code in {"task_not_cancelable", "task_not_found"}:
                raise PanelError("nothing_to_cancel", status=409) from exc
            raise
        self._absorb(session_id, task)
        return self.get(session_id)

    def rename(self, session_id: str, title: str) -> dict[str, Any]:
        """Change the conversation's title (only the title)."""
        clean = _clean_title(title)
        if not clean:
            raise PanelError("bad_title", status=400)
        self._row(session_id)
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (clean, session_id)
            )
        return self.get(session_id)

    def delete(self, session_id: str) -> None:
        """Forget a conversation. The task ledger keeps its own history."""
        with self.db.connect() as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if cursor.rowcount == 0:
                raise SessionNotFound(session_id)
            connection.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            # The task rows stay — they are the protocol's record, not ours — but
            # they must not keep pointing at a conversation that is gone.
            connection.execute(
                "UPDATE tasks SET session_id = NULL WHERE session_id = ?", (session_id,)
            )

    def absorb_task(self, task_id: str) -> bool:
        """Fold a task that changed outside a session call — a push callback.

        Returns True when the task belonged to a conversation. Never raises: a
        callback is an agent's message to us, and a bug in the display layer must
        not turn into a failed delivery the agent then retries forever.
        """
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT id FROM sessions WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return False
        try:
            task = self.tasks.get(task_id)
        except PanelError:
            return False
        self._absorb(row["id"], task)
        return True

    # ------------------------------------------------------------------ reading

    def get(self, session_id: str) -> dict[str, Any]:
        """One conversation with its messages, oldest first."""
        row = self._row(session_id)
        with self.db.connect() as connection:
            messages = connection.execute(
                "SELECT * FROM session_messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return self._view(row, messages)

    def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        limit: int = PAGE_LIMIT,
    ) -> dict[str, Any]:
        """Conversations newest-activity first, optionally for one agent."""
        sql = """
            SELECT s.*,
                   (SELECT text FROM session_messages m
                     WHERE m.session_id = s.id
                     ORDER BY m.id DESC LIMIT 1) AS last_text
              FROM sessions s
        """
        params: list[Any] = []
        if agent_id:
            sql += " WHERE s.agent_id = ?"
            params.append(agent_id)
        # ``rowid`` as the tiebreaker, not ``id``: timestamps have second
        # precision, and two conversations opened in the same second would
        # otherwise be ordered by a random hex suffix.
        sql += " ORDER BY s.updated_at DESC, s.rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), PAGE_LIMIT)))

        with self.db.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return {"items": [self._summary(row) for row in rows]}

    def agent_counts(self, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Conversation counts per agent, in one query."""
        if not agent_ids:
            return {}
        placeholders = ",".join("?" for _ in agent_ids)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT agent_id, COUNT(*) AS n, MAX(updated_at) AS last_at
                  FROM sessions
                 WHERE agent_id IN ({placeholders})
                 GROUP BY agent_id
                """,
                list(agent_ids),
            ).fetchall()
        return {
            row["agent_id"]: {
                "sessionCount": int(row["n"]),
                "lastSessionAt": row["last_at"],
            }
            for row in rows
        }

    def stats(self) -> dict[str, int]:
        """Counts for the summary header."""
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN state IN ('working', 'waiting') THEN 1 ELSE 0 END) AS active
                  FROM sessions
                """
            ).fetchone()
        return {"total": int(row["total"] or 0), "active": int(row["active"] or 0)}

    # ------------------------------------------------------------------ internals

    def _carry(self, row: sqlite3.Row, text: str) -> dict[str, Any]:
        """Send ``text`` into the open task, or start the next turn's task."""
        open_task = self._open_task(row["task_id"])
        if open_task is not None:
            return self.tasks.follow_up(open_task, text)
        return self.tasks.submit(
            row["agent_id"],
            text,
            context_id=row["context_id"],
            session_id=row["id"],
        )

    def _open_task(self, task_id: str | None) -> str | None:
        """The task id, if that task can still take a message."""
        if not task_id:
            return None
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return task_id if state_of(row["state"]) in _OPEN else None

    def _bind(self, session_id: str, task: dict[str, Any]) -> None:
        """Point the conversation at the task that now carries it.

        ``COALESCE`` because the agent may not have assigned a context yet on the
        first turn: the value we already hold is better than overwriting it with
        a NULL.
        """
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                   SET task_id = COALESCE(?, task_id),
                       context_id = COALESCE(?, context_id)
                 WHERE id = ?
                """,
                (task.get("id"), task.get("contextId"), session_id),
            )

    def _absorb(self, session_id: str, task: dict[str, Any]) -> None:
        """Fold one task snapshot into the conversation.

        Two things can arrive: a line of text to append, and a state change to
        record. Either alone is enough to move ``updated_at``; neither alone is
        enough to duplicate a message the user is already reading.
        """
        state = state_of(task.get("state"))
        text = str(task.get("text") or "").strip()
        error = str(task.get("error") or "").strip()
        task_id = task.get("id")

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return
            changed = False
            if error:
                changed = self._append_unique(
                    connection, session_id, "error", error, task_id, FAILED
                )
            elif text:
                changed = self._append_unique(
                    connection, session_id, "agent", text, task_id, state
                )
            if changed or row["state"] != state:
                connection.execute(
                    "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                    (state, iso(), session_id),
                )

    def _remember(self, session_id: str, text: str) -> None:
        """Record the user's line, and let it name the conversation."""
        with self.db.connect() as connection:
            self._insert(connection, session_id, "user", text, None, "sent")
            connection.execute(
                """
                UPDATE sessions
                   SET message_count = message_count + 1,
                       updated_at = ?,
                       title = CASE WHEN TRIM(title) = '' THEN ? ELSE title END
                 WHERE id = ?
                """,
                (iso(), title_from(text), session_id),
            )

    def _append_unique(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        role: str,
        text: str,
        task_id: str | None,
        state: str | None,
    ) -> bool:
        """Append unless it repeats this turn's last line of that role.

        A poll and a push callback routinely deliver the same snapshot twice, and
        an agent that keeps a status message ("working on it") repeats it on every
        update. Comparing with the last message *of the same turn and role* is
        enough and needs no extra column.

        The turn matters: two different turns may legitimately answer with the
        very same words, and collapsing those would silently delete a real reply.
        """
        query = "SELECT text FROM session_messages WHERE session_id = ? AND role = ?"
        params: list[Any] = [session_id, role]
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        query += " ORDER BY id DESC LIMIT 1"
        previous = connection.execute(query, params).fetchone()
        if previous is not None and previous["text"] == text:
            return False
        self._insert(connection, session_id, role, text, task_id, state)
        connection.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        return True

    def _insert(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        role: str,
        text: str,
        task_id: str | None,
        state: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO session_messages (session_id, at, role, text, state, task_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, iso(), role, text, state, task_id),
        )

    def _row(self, session_id: str) -> sqlite3.Row:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return row

    def _view(
        self,
        row: sqlite3.Row,
        messages: list[sqlite3.Row] | None = None,
    ) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": row["id"],
            "agentId": row["agent_id"],
            "agentName": row["agent_name"],
            "title": row["title"],
            "state": row["state"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "messageCount": int(row["message_count"]),
        }
        if messages is not None:
            view["messages"] = [
                {
                    "id": int(message["id"]),
                    "role": message["role"],
                    "text": message["text"],
                    "at": message["at"],
                    "state": message["state"],
                }
                for message in messages
            ]
        return view

    def _summary(self, row: sqlite3.Row) -> dict[str, Any]:
        view = self._view(row)
        view["lastText"] = truncate(row["last_text"], 120) if row["last_text"] else ""
        return view


def _clean_title(title: str | None) -> str:
    """A one-line title, or an empty string (the UI supplies its own label)."""
    return truncate(title, MAX_TITLE) if title else ""

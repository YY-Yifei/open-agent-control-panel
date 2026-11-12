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

import logging
import sqlite3
from typing import Any

from . import media
from .adp import card as cardmod
from .adp.fetcher import FetchError, GuardedFetcher
from .adp.service import RegistryService
from .config import Settings
from .db import Database
from .errors import PanelError
from .tasks import TaskService
from .util import iso, new_id, title_from, truncate

log = logging.getLogger(__name__)

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
        settings: Settings | None = None,
        fetcher: GuardedFetcher | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.tasks = tasks
        # Defaults rather than a required argument: the image limits are the only
        # settings this service reads, and every existing caller — and every test —
        # should keep working without learning about them.
        self.settings = settings or Settings()
        # ``None`` means "this deployment cannot fetch a URL an agent names", and
        # a URL-shaped file part then becomes a chip saying so. That is a real
        # deployment, not a degraded one: a panel that will not make outbound
        # requests from a reply must still show the reply.
        self.fetcher = fetcher

    # ------------------------------------------------------------------ writing

    def create(
        self,
        agent_id: str,
        *,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
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

        if (text and text.strip()) or attachments:
            try:
                return self.send(session_id, text or "", attachments=attachments)
            except PanelError:
                # The row exists already, but a rejected first message must not leave
                # an empty conversation behind: the docstring promises the UI never
                # has to show one. Nothing else was written yet, so undoing is exact.
                self.delete(session_id)
                raise
        return self.get(session_id)

    def send(
        self,
        session_id: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send one user message and record whatever the agent answered.

        The agent call is synchronous, so this returns either the answer or the
        news that the turn is running; a caller that got ``working`` should stop
        asking and wait for the callback (or call :meth:`sync`).

        ``attachments`` are the API's ``{"data", "mime", "name"}`` dicts. They are
        checked *before* anything is written, so a rejected image leaves no
        half-recorded message behind: the conversation gets the whole message or
        none of it.
        """
        body = (text or "").strip()
        prepared = media.prepare_all(
            attachments,
            max_bytes=self.settings.max_image_bytes,
            max_count=self.settings.max_images_per_message,
        )
        if not body and not prepared:
            raise PanelError("empty_message", status=400)

        row = self._row(session_id)
        agent = self.registry.get(row["agent_id"])
        if agent is None:
            raise PanelError("session_agent_gone", status=409, agent_id=row["agent_id"])
        if prepared and cardmod.accepts_images(agent["card"]) is False:
            # The card declares its inputs and none of them is an image. Sending
            # one anyway would spend a full round trip to be refused, and the
            # refusal would arrive as a generic agent error instead of as a fact
            # about the card — which is what the user can actually act on.
            #
            # **Text is not a picture.** A card that declares `["text/plain"]` is
            # exactly the one a `.txt` should reach; folding the two together made
            # a staged text file unsendable (415 on send, with no way back once it
            # is in the composer). So only pictures are refused here.
            if any(item.mime in media.IMAGE_TYPES for item in prepared):
                raise PanelError("agent_no_image_input", status=415, agent_id=row["agent_id"])

        self._remember(session_id, body, prepared)
        task = self._carry(row, body, prepared)
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
            # The pictures of a deleted conversation go with it: they are the
            # panel's copy of what a person attached, not part of the protocol's
            # record, and nothing else will ever ask for them again.
            connection.execute("DELETE FROM session_attachments WHERE session_id = ?", (session_id,))
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
            # The bytes stay in their own table and never travel here: a
            # conversation with ten photographs must not turn into ten megabytes of
            # JSON on every poll. The UI asks ``/attachments/<id>`` for the picture.
            files = connection.execute(
                """
                SELECT id, message_id, mime, name, size, skipped
                  FROM session_attachments
                 WHERE session_id = ?
                 ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return self._view(row, messages, files)

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

    def _carry(
        self,
        row: sqlite3.Row,
        text: str,
        attachments: list[media.Prepared],
    ) -> dict[str, Any]:
        """Send ``text`` and its files into the open task, or start a new one."""
        wire = [_wire(item) for item in attachments] or None
        open_task = self._open_task(row["task_id"])
        if open_task is not None:
            return self.tasks.follow_up(open_task, text, attachments=wire)
        return self.tasks.submit(
            row["agent_id"],
            text,
            context_id=row["context_id"],
            attachments=wire,
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

        Three things can arrive: a line of text to append, files to store, and a
        state change to record. Any one of them alone is enough to move
        ``updated_at``; a snapshot that repeats all three changes nothing, so a
        poll and a push delivering the same update cannot double it.

        Files matter here for one specific reason: an answer that is *only* a
        picture has no text at all, and a rule that keyed on text alone used to
        drop that whole turn on the floor — the bytes were in the ledger, the
        conversation showed nothing.
        """
        state = state_of(task.get("state"))
        text = str(task.get("text") or "").strip()
        error = str(task.get("error") or "").strip()
        task_id = task.get("id")
        if "response" not in task and task_id:
            # Files live in the raw response, and only a detail view carries it.
            # Every caller passes one today; re-reading here means a future caller
            # that does not would lose pictures *silently* — which is the exact
            # failure this whole direction exists to end. One extra read, only in
            # the case that never happens, is the cheap side of that trade.
            try:
                task = self.tasks.get(task_id)
            except PanelError as exc:
                # The whole point of this re-read is not losing files silently;
                # failing to re-read must not be silent either.
                log.warning("task %s could not be re-read for its files: %s", task_id, exc)
        # An error snapshot discards its files below, so do not decode them (or
        # re-attempt unreachable URLs) just to throw the result away.
        files = [] if error else self._inbound(session_id, task_id, task.get("response"))

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return
            changed = False
            if error:
                changed = self._append_unique(
                    connection, session_id, "error", error, task_id, FAILED, []
                )
            elif text or files:
                changed = self._append_unique(
                    connection, session_id, "agent", text, task_id, state, files
                )
            if changed or row["state"] != state:
                connection.execute(
                    "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                    (state, iso(), session_id),
                )

    def _remember(
        self,
        session_id: str,
        text: str,
        attachments: list[media.Prepared],
    ) -> None:
        """Record the user's line and its files, and let them name the conversation."""
        with self.db.connect() as connection:
            message_id = self._insert(connection, session_id, "user", text, None, "sent")
            self._store_media(
                connection,
                session_id,
                message_id,
                [
                    media.Intake(
                        prepared=item,
                        reason=None,
                        mime=item.mime,
                        name=item.name,
                        size=item.size,
                    )
                    for item in attachments
                ],
            )
            connection.execute(
                """
                UPDATE sessions
                   SET message_count = message_count + 1,
                       updated_at = ?,
                       title = CASE WHEN TRIM(title) = '' THEN ? ELSE title END
                 WHERE id = ?
                """,
                (iso(), _title_for(text, attachments), session_id),
            )

    def _append_unique(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        role: str,
        text: str,
        task_id: str | None,
        state: str | None,
        files: list[media.Intake],
    ) -> bool:
        """Append unless it repeats this turn's last message of that role.

        A poll and a push callback routinely deliver the same snapshot twice, and
        an agent that keeps a status message ("working on it") repeats it on every
        update. Comparing with the last message *of the same turn and role* is
        enough and needs no extra column.

        The comparison is (text, files), not text alone: an agent that answers the
        same sentence twice with a different picture each time is saying something,
        and an agent that repeats one picture across two snapshots is not.

        The turn matters: two different turns may legitimately answer with the
        very same words, and collapsing those would silently delete a real reply.
        """
        query = "SELECT id, text FROM session_messages WHERE session_id = ? AND role = ?"
        params: list[Any] = [session_id, role]
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        query += " ORDER BY id DESC LIMIT 1"
        previous = connection.execute(query, params).fetchone()
        if previous is not None and previous["text"] == text:
            # `files` already had every identity of this turn filtered out by
            # :meth:`_inbound`, so "no files left" is exactly the repeat case. The
            # old comparison (previous row's identities == files) could never hold:
            # those identities are all in this turn, so they were removed upstream.
            # Every repeated snapshot therefore appended a second, picture-less copy
            # of the same answer.
            if not files:
                return False

        message_id = self._insert(connection, session_id, role, text, task_id, state)
        self._store_media(connection, session_id, message_id, files)
        connection.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        return True

    # -------------------------------------------------------------- inbound files

    def _inbound(
        self, session_id: str, task_id: str | None, response: Any
    ) -> list[media.Intake]:
        """The files one snapshot carries that this turn has not shown yet.

        Three filters, for three different kinds of repetition:

        * files are deduped **within the turn** by identity. A2A ``artifacts``
          accumulate, so the second snapshot of a task normally still carries the
          first picture; without this the same image would be re-attached to every
          later message of that task. Across turns it is *not* deduped — an agent
          that sends a picture again in a new turn is doing something a person can
          see, and swallowing it would be the panel editing the answer.
        * a file that arrives *by URL* is not downloaded twice in one turn. Its
          identity is only known after the download, so the URL itself is the
          key. Without this, every poll would fetch the same picture again from
          the agent's own server — at the poll rate, for as long as the turn
          lasts.
        * beyond :attr:`Settings.max_inbound_images`, the rest become chips rather
          than being dropped, because "this reply had nine pictures and you are
          seeing four" is information.
        """
        descriptors = cardmod.result_files(response)
        if not descriptors:
            return []

        identities, fetched, shown = self._personal_files(session_id, task_id)
        limit = max(1, int(self.settings.max_inbound_images))
        max_bytes = int(self.settings.max_inbound_image_bytes)

        # The quota is per *turn*, not per snapshot. A poll re-reads the very same
        # artifacts, so a counter that starts at zero would accept them again on the
        # next snapshot — overshooting the limit and appending a second message with
        # the same text. Counting what the turn already shows is what makes the
        # quota mean something across polls.
        taken = shown
        attempts = 0
        seen: set[str] = set(identities)
        out: list[media.Intake] = []
        for item in descriptors:
            mime = str(item.get("mime") or "")
            name = str(item.get("name") or "")
            uri = item.get("uri")
            inline = item.get("data")
            if not inline and not uri:
                continue
            if uri and uri in fetched:
                # Those bytes are already stored for this turn.
                continue
            if inline:
                got = media.intake_b64(
                    inline, declared=mime, name=name, max_bytes=max_bytes
                )
                if got.sha256 in seen:
                    # The same picture again (`artifacts` accumulate): neither a
                    # second copy nor a "too many" chip is warranted.
                    continue
                if taken >= limit:
                    got = media.skip(
                        media.SKIP_TOO_MANY,
                        declared=mime,
                        name=name,
                        source=uri,
                        size=got.size,
                        digest=got.sha256,
                    )
            elif taken >= limit or attempts >= limit:
                # Decide the overflow *before* downloading: fetching only to turn
                # the bytes into a chip lets an agent force as many outbound
                # requests as it likes, whatever the limit says. A failed fetch
                # costs a request too, so attempts are budgeted, not successes.
                got = media.skip(media.SKIP_TOO_MANY, declared=mime, name=name, source=uri)
            else:
                attempts += 1
                got = self._fetch_file(uri, declared=mime, name=name, max_bytes=max_bytes)
            if uri:
                # The same URL may appear twice in one response (artifacts and the
                # status message are read together). Remember it so one poll never
                # downloads the same file twice.
                fetched.add(uri)
            if got.sha256 in seen:
                continue
            seen.add(got.sha256)
            if got.shown:
                taken += 1
            out.append(got)
        return out

    def _fetch_file(
        self, uri: str, *, declared: str, name: str, max_bytes: int
    ) -> media.Intake:
        """Fetch one file an agent pointed at, through the guarded fetcher.

        A URL from an agent is a URL from a stranger: it goes through exactly the
        same guard a card URL does (public addresses only, DNS pinned after
        validation, redirects re-checked hop by hop, body capped while streaming).
        Nothing about "the agent is already registered" makes its next URL safe.
        """
        label = name or media.name_from_url(uri)
        # "*/*" 与 octet-stream 都是「我不知道是什么」，和空声明一样，该让字节说话：
        # accepts_images 已经把 "*/*" 读作「可能收图」，两边不能各说各话。
        #
        # 这道门是「值不值得下载」，不是「收不收」：0.5.0 时目录里只有图片，
        # 所以凡声明不是 image/* 的都在下载前拒掉了。现在文本与 PDF 也会被存
        # 下来（预览要用），于是判据变成 media.accepts_document —— ZIP 之类
        # 仍然在下载前拒：抓回来只会确认一遍它自己给的标签。
        if not media.accepts_document(declared):
            return media.skip(media.SKIP_UNSUPPORTED, declared=declared, name=label, source=uri)
        if self.fetcher is None:
            return media.skip(media.SKIP_UNREACHABLE, declared=declared, name=label, source=uri)
        try:
            document = self.fetcher.fetch(uri, expect_json=False)
        except FetchError as exc:
            # "too large" will not shrink on the next poll, so it must not be retried
            # at the poll rate; only a real reachability failure earns another go.
            reason = (
                media.SKIP_TOO_LARGE
                if exc.code == "CARD_TOO_LARGE"
                else media.SKIP_UNREACHABLE
            )
            return media.skip(reason, declared=declared, name=label, source=uri)
        return media.intake(
            document.body,
            declared=declared,
            name=label,
            max_bytes=max_bytes,
            source=uri,
        )

    def _personal_files(
        self, session_id: str, task_id: str | None
    ) -> tuple[set[str], set[str], int]:
        """What this turn already holds: file identities, and URLs whose bytes it has.

        Identities are what stop a file from being attached twice; the URL set is
        what stops one from being *downloaded* twice; the count of shown files is
        what carries the quota across snapshots. A URL counts as attempted
        unless reaching it failed: "too large", "unsupported" and "bad data" cannot
        turn into a picture on a retry, but a CDN that was briefly down can — so
        only ``unreachable`` is retried, at the poll rate, no more.
        """
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.sha256, a.source, a.skipped
                  FROM session_attachments a
                  JOIN session_messages m ON m.id = a.message_id
                 WHERE m.session_id = ? AND m.task_id IS ?
                 ORDER BY a.id ASC
                """,
                (session_id, task_id),
            ).fetchall()
        return (
            {row["sha256"] for row in rows},
            {
                row["source"]
                for row in rows
                if row["source"] and row["skipped"] != media.SKIP_UNREACHABLE
            },
            sum(1 for row in rows if not row["skipped"]),
        )

    def _store_media(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        message_id: int,
        items: list[media.Intake],
    ) -> None:
        """Write the files of one message. A skipped one keeps its row, not bytes."""
        if not items:
            return
        now = iso()
        connection.executemany(
            """
            INSERT INTO session_attachments
                (session_id, message_id, at, mime, name, size, sha256, data, skipped,
                 source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    message_id,
                    now,
                    item.mime,
                    item.name,
                    item.size,
                    item.sha256,
                    item.prepared.data if item.prepared is not None else b"",
                    item.reason,
                    item.source,
                )
                for item in items
            ],
        )

    def _insert(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        role: str,
        text: str,
        task_id: str | None,
        state: str | None,
    ) -> int:
        """Insert one message and return its row id (attachments hang off it)."""
        cursor = connection.execute(
            """
            INSERT INTO session_messages (session_id, at, role, text, state, task_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, iso(), role, text, state, task_id),
        )
        return int(cursor.lastrowid or 0)

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
        files: list[sqlite3.Row] | None = None,
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
            by_message: dict[int, list[dict[str, Any]]] = {}
            for item in files or []:
                by_message.setdefault(int(item["message_id"] or 0), []).append(
                    {
                        "id": int(item["id"]),
                        "mime": item["mime"],
                        "name": item["name"],
                        "size": int(item["size"]),
                        # NULL for a file that can be shown; a reason code for one
                        # that cannot. The UI turns the code into words, so the
                        # reason is localized when it is read, not when it is stored.
                        "skipped": item["skipped"],
                    }
                )
            view["messages"] = [
                {
                    "id": int(message["id"]),
                    "role": message["role"],
                    "text": message["text"],
                    "at": message["at"],
                    "state": message["state"],
                    "attachments": by_message.get(int(message["id"]), []),
                }
                for message in messages
            ]
        return view

    def _summary(self, row: sqlite3.Row) -> dict[str, Any]:
        view = self._view(row)
        view["lastText"] = truncate(row["last_text"], 120) if row["last_text"] else ""
        return view


def _wire(item: media.Prepared) -> dict[str, Any]:
    """One prepared attachment in the shape the task ledger and the client speak."""
    return {
        "data": item.b64,
        "mime": item.mime,
        "name": item.name,
        "size": item.size,
        "sha256": item.sha256,
    }


def _title_for(text: str, attachments: list[media.Prepared]) -> str:
    """A conversation name taken from the first thing the message contains.

    An image-only message has no text to name itself with, so it borrows the file
    name. Without this the first thing a user ever sends would leave the title
    empty and the sidebar would keep saying "new conversation" about a
    conversation that plainly is not new.
    """
    if text.strip():
        return title_from(text)
    if attachments:
        return title_from(attachments[0].name)
    return ""


def _clean_title(title: str | None) -> str:
    """A one-line title, or an empty string (the UI supplies its own label)."""
    return truncate(title, MAX_TITLE) if title else ""

"""SQLite persistence for agents, registrations, conversations and tasks.

One connection per operation: the panel is a small control plane, so the cost is
irrelevant next to the simplicity of never sharing a connection across threads.
Write-ahead logging keeps readers from blocking the probe loop.

Two layers live here on purpose. ``tasks`` is the ledger of what was actually
exchanged with an agent over A2A; ``sessions``/``session_messages`` is the
conversation the operator sees. The second is derived from the first — every
message row points at the task it came from — but it is stored rather than
computed, because a conversation has to survive the task row being pruned and
because the UI must not have to know what a task is.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA_VERSION = 8


class MigrationError(RuntimeError):
    """An on-open schema upgrade refused to continue rather than lose a row."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    host            TEXT NOT NULL,
    card            TEXT NOT NULL,
    card_hash       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'down')),
    first_seen_at   TEXT NOT NULL,
    checked_at      TEXT NOT NULL,
    last_ok_at      TEXT NOT NULL,
    failures        INTEGER NOT NULL DEFAULT 0,
    changed_at      TEXT,
    prev_hash       TEXT,
    manage_token    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS agents_first_seen ON agents (first_seen_at DESC, id ASC);
CREATE INDEX IF NOT EXISTS agents_host ON agents (host);
CREATE INDEX IF NOT EXISTS agents_status ON agents (status);

CREATE TABLE IF NOT EXISTS registrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip          TEXT NOT NULL,
    host        TEXT,
    at          TEXT NOT NULL,
    outcome     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS registrations_ip ON registrations (ip, at);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    agent_url       TEXT NOT NULL,
    endpoint        TEXT,
    remote_task_id  TEXT,
    context_id      TEXT,
    state           TEXT NOT NULL,
    title           TEXT NOT NULL,
    input_text      TEXT NOT NULL,
    request_json    TEXT,
    response_json   TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    synced_at       TEXT,
    message_count   INTEGER NOT NULL DEFAULT 0,
    push_token      TEXT,
    push_state      TEXT,
    push_registered_at TEXT,
    push_count      INTEGER NOT NULL DEFAULT 0,
    last_push_at    TEXT,
    session_id      TEXT
);

CREATE INDEX IF NOT EXISTS tasks_agent ON tasks (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_created ON tasks (created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks (state);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT
);

CREATE INDEX IF NOT EXISTS task_events_task ON task_events (task_id, id);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    agent_url       TEXT NOT NULL,
    title           TEXT NOT NULL,
    context_id      TEXT,
    task_id         TEXT,
    state           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS sessions_agent ON sessions (agent_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS sessions_updated ON sessions (updated_at DESC);

CREATE TABLE IF NOT EXISTS session_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    at          TEXT NOT NULL,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL,
    state       TEXT,
    task_id     TEXT
);

CREATE INDEX IF NOT EXISTS session_messages_session ON session_messages (session_id, id);

CREATE TABLE IF NOT EXISTS session_attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    message_id  INTEGER,
    at          TEXT NOT NULL,
    mime        TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    data        BLOB NOT NULL,
    skipped     TEXT,
    source      TEXT
);

CREATE INDEX IF NOT EXISTS session_attachments_session ON session_attachments (session_id, id);
CREATE INDEX IF NOT EXISTS session_attachments_message ON session_attachments (message_id, id);
"""

#: Task columns added after schema v1 (push notifications) and v2 (sessions).
#: ``CREATE TABLE IF NOT EXISTS`` never touches a table that already exists, so a
#: database written by an older release has to be widened in place rather than
#: recreated.
_ADDED_TASK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("push_token", "TEXT"),
    ("push_state", "TEXT"),
    ("push_registered_at", "TEXT"),
    ("push_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_push_at", "TEXT"),
    ("session_id", "TEXT"),
)


class Database:
    """A thin owner of a SQLite file."""

    def __init__(self, path: str) -> None:
        self.path = str(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with row access by name and commit-on-success."""
        connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def init(self) -> None:
        """Create the schema (idempotent), widen an old one, and enable WAL."""
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            previous = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.executescript(_SCHEMA)
            _widen(connection)
            _rekey_agents(connection, previous)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def ping(self) -> bool:
        """Return True when the database answers a trivial query."""
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


def _widen(connection: sqlite3.Connection) -> None:
    """Add the columns a pre-0.3 database is missing, then index the new ones.

    The indexes are created here rather than in ``_SCHEMA`` on purpose: on an old
    file the column does not exist yet when ``executescript`` runs, and
    ``CREATE INDEX`` on a missing column is an error, not a no-op.
    """
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
    for name, declaration in _ADDED_TASK_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
    connection.execute("CREATE INDEX IF NOT EXISTS tasks_push_token ON tasks (push_token)")
    connection.execute("CREATE INDEX IF NOT EXISTS tasks_session ON tasks (session_id)")

    # Schema 6 added the table (the outbound-only build); schema 7 added two
    # columns. ``skipped``: an inbound file the panel will not show is still a row,
    # so a conversation can say why a picture is missing instead of pretending
    # nothing arrived. ``source``: a file that came from a URL remembers that URL,
    # so the next poll does not download it again. A row that never needed either
    # keeps NULL, which is what the ALTER gives an existing file.
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(session_attachments)")}
    for name, declaration in (("skipped", "TEXT"), ("source", "TEXT")):
        if existing and name not in existing:
            connection.execute(f"ALTER TABLE session_attachments ADD COLUMN {name} {declaration}")


def _rekey_agents(connection: sqlite3.Connection, previous: int) -> None:
    """Re-derive ids from ``(host, card name)`` (schema 7 -> 8).

    Every release before this one keyed an entry by ``sha256(normalised url)``. That
    made a port change a *new agent*: an operator restarting a local agent on another
    port got a new id, and the conversations pointing at the old one became
    unreachable. Schema 8 keys on the host without its port plus the card's name, so
    the same agent coming back on a new port is the same entry.

    Two things make this pass harder than the URL one it replaces.

    **Several rows can land on one id.** That is not a collision, it is the point: a
    file written across several port changes holds several rows for one agent. §10
    says a collision must stop rather than merge, and merging here would also throw
    away a row's manage token, so nothing is deleted. The group is ordered by the
    row's own attributes — a live one first, then the most recent success, then the
    url as a tie-break — and the first member takes the derived id while the rest
    take ``<id>_2``, ``<id>_3``, … . They stay addressable and deletable; they are
    simply no longer the entry a registration resolves to.

    **The ordering must not depend on ``id``**, because this pass rewrites ids and so
    has to be idempotent. The writes themselves run in one explicit transaction, so an
    interruption rolls the whole pass back and leaves ``user_version`` at 7 — the next
    start recomputes the same mapping from the same attributes. Without it, a crash
    between a row's id change and its conversations' re-point would leave sessions
    pointing at an id that no longer exists, and a rerun could not repair that: the row
    already sits at its target id and is no longer part of the move.

    Two *different* groups deriving one id are a real collision (the 64-bit caveat in
    §4.1). That aborts the pass before anything is written, which keeps §10's meaning
    for the accidental case.

    Conversations and tasks point at the agent id, so they move with it. A dangling
    reference — an entry deleted while its conversations stayed — keeps the old id:
    re-keying it would point it at an unrelated agent.
    """
    if previous >= 8:
        return

    from .net import derive_id, name_key, path_of  # local: ``net`` stays storage-free
    from .util import iso

    rows = connection.execute(
        "SELECT id, url, host, card, status, last_ok_at FROM agents"
    ).fetchall()
    if not rows:
        return

    def _name(row: sqlite3.Row) -> str:
        try:
            payload = json.loads(row["card"])
        except (TypeError, ValueError):
            return ""
        return str(payload.get("name") or "") if isinstance(payload, dict) else ""

    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        # Folded exactly as ``derive_id`` folds it: a stored host differing only in
        # case must join its own group instead of deriving the same id in a second
        # one, which would read as a collision and abort the pass. The path is part of
        # the group for the same reason it is part of the key.
        groups.setdefault(
            (row["host"].casefold(), path_of(row["url"]), name_key(_name(row))), []
        ).append(row)

    mapping: dict[str, str] = {}
    claimed: dict[str, tuple[str, str]] = {}
    for (host, path, _folded), members in groups.items():
        ordered = sorted(members, key=lambda item: item["url"])
        ordered.sort(key=lambda item: item["last_ok_at"], reverse=True)
        ordered.sort(key=lambda item: item["status"] != "ok")
        base = derive_id(host, path, _name(ordered[0]))
        if base in claimed:
            first = claimed[base]
            raise MigrationError(
                "cannot re-key agents: two different hosts and names derive the same "
                f"id ({first[0]!r} / {first[1]!r} and {host!r} / {_name(ordered[0])!r} "
                f"-> {base!r}); nothing was merged — resolve the collision or start a "
                "fresh database"
            )
        claimed[base] = (host, _name(ordered[0]))
        for index, row in enumerate(ordered):
            mapping[row["id"]] = base if index == 0 else f"{base}_{index + 1}"

    moving = {old: new for old, new in mapping.items() if old != new}
    if not moving:
        return

    # Two passes. A target id may currently belong to another row in this same pass,
    # so every moving row parks under an id that cannot collide (``~`` is not allowed
    # in a contract id) before any of them takes its final one.
    parked = {old_id: f"~tmp~{index}" for index, old_id in enumerate(moving)}
    # Dependants need parking of their own, for a different reason: nothing makes
    # ``sessions.agent_id`` unique, so re-pointing one is a sweep by value, and one
    # row's target id can be another row's source id. Sweeping in place would drag a
    # conversation that was just re-pointed onto an unrelated agent — silently, and in
    # a way no rerun can repair.
    seats = {old_id: f"~seat~{index}" for index, old_id in enumerate(moving)}
    connection.execute("BEGIN IMMEDIATE")
    try:
        for old_id, temporary in parked.items():
            connection.execute("UPDATE agents SET id = ? WHERE id = ?", (temporary, old_id))
        for old_id, seat in seats.items():
            connection.execute(
                "UPDATE sessions SET agent_id = ? WHERE agent_id = ?", (seat, old_id)
            )
            connection.execute("UPDATE tasks SET agent_id = ? WHERE agent_id = ?", (seat, old_id))
        for old_id, new_id in moving.items():
            connection.execute("UPDATE agents SET id = ? WHERE id = ?", (new_id, parked[old_id]))
        for old_id, new_id in moving.items():
            connection.execute(
                "UPDATE sessions SET agent_id = ? WHERE agent_id = ?", (new_id, seats[old_id])
            )
            connection.execute(
                "UPDATE tasks SET agent_id = ? WHERE agent_id = ?", (new_id, seats[old_id])
            )
        connection.execute(
            "INSERT INTO registrations (ip, host, at, outcome) VALUES (?, NULL, ?, ?)",
            ("migration", iso(), "rekeyed"),
        )
        connection.execute("COMMIT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

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

SCHEMA_VERSION = 5

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


def _rekey_agents(connection: sqlite3.Connection, previous: int) -> None:
    """Re-derive ids from the stored URL (schema 4 → 5).

    Schema 4 keyed an entry by ``sha256(card name)``; ADP §4.1 keys it by
    ``sha256(normalised url)``. A file written by 0.3.0 therefore has to be
    re-keyed once, or every stored entry would look unregistered and its URL —
    still unique — would be unregisterable. Conversations and tasks point at the
    agent id, so they are re-pointed in the same pass.

    The name rule was a local invention: the published contract says two URLs
    serving one card are two entries, and it does not let a registry decide that
    a rename is a different agent. Schema 4 existed for one release; this pass
    takes those files back to the rule every other release has used.

    Each statement autocommits (``isolation_level=None``), which is why the pass
    has to be idempotent: an interrupted run leaves ``user_version`` at 4 and the
    next start finishes the job without moving anything twice.
    """
    if previous >= SCHEMA_VERSION:
        return

    from .net import derive_id  # local: ``net`` must stay ignorant of storage
    from .util import iso

    rows = connection.execute("SELECT id, url FROM agents").fetchall()
    moved = False
    for row in rows:
        old_id = row["id"]
        new_id = derive_id(row["url"])
        if new_id == old_id:
            continue
        moved = True

        survivor = connection.execute("SELECT 1 FROM agents WHERE id = ?", (new_id,)).fetchone()
        if survivor is not None:
            connection.execute("DELETE FROM agents WHERE id = ?", (old_id,))
        else:
            connection.execute("UPDATE agents SET id = ? WHERE id = ?", (new_id, old_id))
        # Repointing before or after the agents row does not matter: neither table
        # declares a foreign key, and both orders leave the same result.
        connection.execute("UPDATE sessions SET agent_id = ? WHERE agent_id = ?", (new_id, old_id))
        connection.execute("UPDATE tasks SET agent_id = ? WHERE agent_id = ?", (new_id, old_id))

    if moved:
        connection.execute(
            "INSERT INTO registrations (ip, host, at, outcome) VALUES (?, NULL, ?, ?)",
            ("migration", iso(), "rekeyed"),
        )

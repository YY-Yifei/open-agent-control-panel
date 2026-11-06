"""SQLite persistence for agents, registration attempts and tasks.

One connection per operation: the panel is a small control plane, so the cost is
irrelevant next to the simplicity of never sharing a connection across threads.
Write-ahead logging keeps readers from blocking the probe loop.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA_VERSION = 1

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
    message_count   INTEGER NOT NULL DEFAULT 0
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
"""


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
        """Create the schema (idempotent) and enable WAL when possible."""
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def ping(self) -> bool:
        """Return True when the database answers a trivial query."""
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

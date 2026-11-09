"""Schema migration: a database written by 0.2.x must widen in place.

Every other test starts from an empty file, where ``CREATE TABLE`` already has
the new columns and the migration has nothing to do. This one starts from the
*v1* shape, which is the only situation real operators will be in.
"""

from __future__ import annotations

import sqlite3

from oacp.db import SCHEMA_VERSION, Database

#: The ``tasks`` table exactly as 0.2.x created it: no push columns at all.
V1_TASKS = """
CREATE TABLE tasks (
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
INSERT INTO tasks (id, agent_id, agent_name, agent_url, state, title,
                   input_text, created_at, updated_at, message_count)
VALUES ('t_old', 'a_1', 'Old Agent', 'https://old.example.com/a2a', 'completed',
        'an old task', 'an old prompt', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1);
"""


def make_v1_database(path: str) -> None:
    """Write a database that looks like one from before push notifications."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_TASKS)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def columns(path: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    finally:
        connection.close()


def test_old_database_gains_the_push_columns(tmp_path) -> None:
    path = str(tmp_path / "old.db")
    make_v1_database(path)
    assert "push_token" not in columns(path)

    Database(path).init()

    widened = columns(path)
    assert {"push_token", "push_state", "push_registered_at", "push_count", "last_push_at"} <= widened
    connection = sqlite3.connect(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        row = connection.execute(
            "SELECT title, push_count, push_state FROM tasks WHERE id = 't_old'"
        ).fetchone()
    finally:
        connection.close()
    assert version == SCHEMA_VERSION
    assert row == ("an old task", 0, None), "existing rows must survive with sane defaults"


def test_migration_is_idempotent(tmp_path) -> None:
    path = str(tmp_path / "old.db")
    make_v1_database(path)

    for _ in range(3):
        Database(path).init()

    assert {"push_token", "push_count"} <= columns(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        connection.close()


def test_callback_token_is_indexed(tmp_path) -> None:
    """The callback lookup is by token, so the index has to exist after migration."""
    path = str(tmp_path / "old.db")
    make_v1_database(path)
    Database(path).init()

    connection = sqlite3.connect(path)
    try:
        indexes = [row[1] for row in connection.execute("PRAGMA index_list(tasks)")]
    finally:
        connection.close()
    assert "tasks_push_token" in indexes

"""The read-only inspection layer: what it shows, and what it refuses.

Two things are under test here, and they are different in kind:

* the **answers** — a describe that names the columns, a query that renders a
  table, a truncation that says it truncated. These are what make the capability
  usable at all.
* the **guarantee** — a write fails, a secret comes back masked, a runaway
  statement stops. These are what let the capability honestly claim the ``read``
  tier, so they are asserted against the connection rather than the wrapper.
  A test that only checked ``query("DELETE …")`` returning ``ok=False`` would
  pass even if the masking came from the wrapper; here the raw ``execute`` is
  tried too, because that is where the guarantee actually lives.
"""

from __future__ import annotations

import sqlite3

import pytest

from oacp import db_inspect
from oacp.db import Database


@pytest.fixture()
def database(tmp_path) -> tuple[str, Database]:
    path = str(tmp_path / "oacp.db")
    store = Database(path)
    store.init()
    return path, store


@pytest.fixture()
def seeded(database):
    """A small ledger: three tasks, two states, one agent with a token."""
    path, store = database
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO agents (id, url, host, card, card_hash, status, first_seen_at,"
            " checked_at, last_ok_at, failures, manage_token)"
            " VALUES ('a_1','http://h:1','h','{}','hash','ok',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',0,'TOKEN-SECRET')"
        )
        for index, state in enumerate(("completed", "completed", "failed")):
            connection.execute(
                "INSERT INTO tasks (id, agent_id, agent_name, agent_url, state, title,"
                " input_text, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"t_{index}",
                    "a_1",
                    "Geo Agent",
                    "http://h:1",
                    state,
                    "lookup",
                    "where",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:05:00Z",
                ),
            )
        connection.execute(
            "INSERT INTO session_attachments (session_id, at, mime, name, size, sha256, data)"
            " VALUES ('s_1','2026-01-01T00:00:00Z','image/png','a.png',4,'abc',?)",
            (b"\x89PNG",),
        )
    return path, store


@pytest.fixture()
def connection(seeded):
    path, _ = seeded
    opened = db_inspect.open_readonly(path)
    try:
        yield opened
    finally:
        opened.close()


# ------------------------------------------------------------------ the guarantee


def test_write_fails_at_the_connection_not_in_the_wrapper(connection):
    """``mode=ro`` is the tier's proof: a write is refused by SQLite itself."""
    with pytest.raises(sqlite3.Error):
        connection.execute("UPDATE agents SET status = 'down'")


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM tasks",
        "UPDATE tasks SET state = 'cancelled'",
        "INSERT INTO tasks (id) VALUES ('x')",
        "DROP TABLE tasks",
        "ALTER TABLE tasks ADD COLUMN nope TEXT",
        "CREATE TABLE nope (id TEXT)",
        "PRAGMA journal_mode = DELETE",
    ],
)
def test_write_statements_are_refused(connection, statement):
    answer = db_inspect.query(connection, statement)
    assert answer.ok is False
    assert "SQL_ERROR" in answer.text


def test_a_refused_statement_never_says_it_ran(connection):
    """``sql ok`` must not appear in a failure, or the model reports success."""
    answer = db_inspect.query(connection, "DELETE FROM tasks")
    assert not answer.text.startswith("sql ok")


def test_a_statement_that_reads_a_credential_is_refused(connection):
    """Not masked — refused.

    Masking the cell leaves the row count, the ordering and whether a WHERE clause
    matched still visible, which is an oracle: a caller can recover a token one
    comparison at a time without the secret ever appearing in a cell. SQLite's
    authorizer answers SQLITE_DENY instead, so the statement cannot be evaluated.
    """
    for statement in (
        "SELECT manage_token FROM agents",
        "SELECT * FROM agents",
        "SELECT id, host FROM agents WHERE manage_token = 'guess'",
        "SELECT count(*) FROM agents WHERE substr(manage_token, 1, 2) = 'TO'",
        "SELECT id FROM agents ORDER BY manage_token",
    ):
        answer = db_inspect.query(connection, statement)
        assert answer.ok is False, statement
        assert "credential column" in answer.text, statement
        assert "TOKEN-SECRET" not in answer.text, statement


def test_an_alias_does_not_get_around_the_refusal(connection):
    """The check is on what the statement *reads*, not what it calls it.

    A name-keyed check is one `AS` away from being decorative: the output column is
    called `tok`, the value is still the credential. SQLite's authorizer reports
    every column a statement references, which is the only place that fact exists.
    """
    answer = db_inspect.query(connection, "SELECT manage_token AS tok FROM agents")
    assert answer.ok is False
    assert "TOKEN-SECRET" not in answer.text


def test_an_expression_does_not_get_around_the_refusal(connection):
    """`substr(push_token,1,20)` is the same exfiltration with extra steps."""
    answer = db_inspect.query(connection, "SELECT substr(manage_token,1,5) AS x FROM agents")
    assert answer.ok is False
    assert "TOKEN" not in answer.text


def test_the_header_is_not_an_oracle(connection):
    """The hole that made masking the wrong fix (round six).

    Every secret cell can be hashed and the answer still leaks: `count(*)` over a
    guessed value tells the caller whether the guess matched. One byte at a time is
    all a token needs. This is why the read is refused rather than redacted.
    """
    guess = db_inspect.query(
        connection, "SELECT count(*) FROM agents WHERE manage_token = 'TOKEN-SECRET'"
    )
    wrong = db_inspect.query(
        connection, "SELECT count(*) FROM agents WHERE manage_token = 'nope'"
    )
    # Both are refused, so neither answers "how many matched".
    assert guess.ok is False
    assert wrong.ok is False
    assert "1" not in guess.text.splitlines()[0]


def test_a_query_that_never_touches_a_credential_is_not_masked(connection):
    """The blunt path must not fire on ordinary questions, or the mask is useless."""
    answer = db_inspect.query(connection, "SELECT id, host, status FROM agents")
    assert "a_1" in answer.text
    assert "sha256:" not in answer.text


def test_two_statements_are_refused_instead_of_raising(connection):
    """``sqlite3.Warning`` is not a ``sqlite3.Error``: uncaught, this is a 500."""
    answer = db_inspect.query(connection, "SELECT 1; SELECT 2")
    assert answer.ok is False
    assert "one statement at a time" in answer.text


def test_a_runaway_statement_is_stopped(connection):
    """A recursive CTE does not fail on its own — it just keeps going."""
    answer = db_inspect.query(
        connection,
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n)"
        " SELECT count(*) FROM n",
        budget_ms=120,
    )
    assert answer.ok is False
    assert "SQL_TIMEOUT" in answer.text


# ------------------------------------------------------------------- the answers


def test_describe_lists_every_table_with_its_columns(connection):
    text = db_inspect.describe(connection)
    for table in ("agents", "tasks", "sessions", "session_messages", "task_events", "registrations"):
        assert table in text
    assert "manage_token" in text
    # The one structural fact that makes every time query writable.
    assert "YYYY-MM-DDTHH:MM:SSZ" in text


def test_describe_marks_credentials(connection):
    assert "manage_token†" in db_inspect.describe(connection)


def test_describe_reads_enumeration_values_from_the_data(connection):
    """Values come from the file, so a state this release never heard of shows up."""
    text = db_inspect.describe(connection, "tasks")
    assert "completed=2" in text
    assert "failed=1" in text


def test_describe_reports_indexes(connection):
    text = db_inspect.describe(connection, "tasks")
    assert "tasks_state" in text


def test_describe_of_an_unknown_table_suggests_the_real_one(connection):
    text = db_inspect.describe(connection, "task")
    assert "no such table" in text
    assert "tasks" in text


def test_query_renders_a_table_not_json(connection):
    answer = db_inspect.query(
        connection, "SELECT state, count(*) AS n FROM tasks GROUP BY 1 ORDER BY 2 DESC"
    )
    assert answer.ok is True
    lines = answer.text.splitlines()
    assert lines[0].startswith("sql ok")
    assert "state" in lines[1] and "n" in lines[1]
    assert "completed" in answer.text


def test_query_says_how_many_rows_matched(connection):
    """A caller must be able to tell a full answer from a page of one.

    Only ``limit + 1`` rows are fetched, so the count is "of at least 3", not an
    exact total: scanning to get an exact number meant holding every BLOB of every
    row in memory, which is the one thing the fetch loop exists to avoid.
    ``count(*)`` is how a caller asks for the real total.
    """
    answer = db_inspect.query(connection, "SELECT id FROM tasks", limit=2)
    assert "2 rows of at least 3" in answer.text
    assert "there are more rows" in answer.text


def test_an_empty_result_is_not_an_error(connection):
    answer = db_inspect.query(connection, "SELECT id FROM tasks WHERE state = 'nope'")
    assert answer.ok is True
    assert "0 rows" in answer.text
    assert "not an error" in answer.text


def test_a_misspelled_column_comes_back_with_a_hint(connection):
    """Without the hint a model retries agentName, agent_name, name — and quits."""
    answer = db_inspect.query(connection, "SELECT agentname FROM tasks")
    assert answer.ok is False
    assert "no such column" in answer.text
    assert "agent_name" in answer.text


def test_pragma_is_allowed(connection):
    """``PRAGMA table_info`` / ``integrity_check`` are exactly what a reader wants."""
    answer = db_inspect.query(connection, "PRAGMA integrity_check")
    assert answer.ok is True
    assert "ok" in answer.text


def test_a_blob_is_rendered_as_a_stub(connection):
    """Otherwise one ``SELECT *`` spends the whole result budget on bytes."""
    answer = db_inspect.query(connection, "SELECT name, data FROM session_attachments")
    assert "<blob 4 B>" in answer.text
    assert "PNG" not in answer.text


def test_a_newline_inside_a_cell_does_not_break_the_table(seeded):
    """``input_text`` and ``error`` are full of newlines; a real one would shift
    every following row and make the table unreadable exactly where it matters."""
    path, store = seeded
    with store.connect() as writer:
        writer.execute("UPDATE tasks SET input_text = 'line1\nline2\ttabbed' WHERE id = 't_0'")

    opened = db_inspect.open_readonly(path)
    try:
        answer = db_inspect.query(opened, "SELECT id, input_text FROM tasks WHERE id = 't_0'")
    finally:
        opened.close()
    assert answer.ok is True
    assert "line1 line2 tabbed" in answer.text
    assert "\t" not in answer.text
    # Every rendered line must belong to exactly one row.
    body = answer.text.splitlines()[1:]
    assert len(body) == 2, body


def test_a_wide_result_fits_the_budget_without_losing_its_notes(seeded):
    """The catalog clips at 4000; the renderer must fit first, or the clip eats
    the "N rows of M" line that tells a caller it is looking at a page."""
    path, store = seeded
    with store.connect() as writer:
        writer.executemany(
            "INSERT INTO tasks (id, agent_id, agent_name, agent_url, state, title,"
            " input_text, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (f"w_{index}", "a_1", "Geo Agent", "http://h:1", "completed", "t", "x",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z")
                for index in range(300)
            ],
        )

    opened = db_inspect.open_readonly(path)
    try:
        answer = db_inspect.query(
            opened,
            "SELECT id, agent_id, agent_name, agent_url, state, title, input_text,"
            " created_at, updated_at FROM tasks",
            limit=500,
        )
    finally:
        opened.close()
    assert answer.ok is True
    assert len(answer.text) <= 4000, len(answer.text)
    # Fewer rows, not a truncated sentence: the header still says how many matched,
    # and the note says the cut was the budget rather than a row limit.
    assert "rows of" in answer.text.splitlines()[0]
    assert "answer budget" in answer.text


def test_a_blob_table_does_not_materialise_every_image(seeded):
    """A row cap bounds rows, not bytes: 5 MB per attachment × rows is an OOM.

    The fetch stops on ``MAX_RESULT_BYTES``, so the answer says it showed fewer
    rows rather than the process dying on the way to the answer.
    """
    path, store = seeded
    big = b"x" * (1024 * 1024)
    with store.connect() as writer:
        writer.executemany(
            "INSERT INTO session_attachments (session_id, at, mime, name, size, sha256, data)"
            " VALUES ('s_1','2026-01-01T00:00:00Z','image/png',?,?, 'abc', ?)",
            [(f"big{index}.png", len(big), big) for index in range(20)],
        )

    opened = db_inspect.open_readonly(path)
    try:
        answer = db_inspect.query(opened, "SELECT name, data FROM session_attachments", limit=20)
    finally:
        opened.close()
    assert answer.ok is True
    # And it says the size budget stopped it, so 8 rows is not read as "only 8".
    assert "size budget" in answer.text


def test_limit_is_capped(connection):
    answer = db_inspect.query(connection, "SELECT id FROM tasks", limit=10_000)
    assert answer.ok is True
    assert len(answer.text.splitlines()) < 20


def test_empty_statement_is_refused(connection):
    answer = db_inspect.query(connection, "   ")
    assert answer.ok is False
    assert "SQL_ERROR" in answer.text


def test_a_comment_before_the_keyword_is_still_classified(connection):
    """Leading comments must not let a write slip past the head check."""
    answer = db_inspect.query(connection, "-- anything\nDELETE FROM tasks")
    assert answer.ok is False


# ------------------------------------------------------------------ opening files


def test_a_missing_file_is_a_read_only_error(tmp_path):
    with pytest.raises(db_inspect.ReadOnlyError) as caught:
        db_inspect.open_readonly(str(tmp_path / "nope.db"))
    assert "not found" in str(caught.value)


def test_a_directory_is_not_a_database(tmp_path):
    with pytest.raises(db_inspect.ReadOnlyError):
        db_inspect.open_readonly(str(tmp_path))


def test_an_empty_but_existing_file_describes_nothing(database):
    """A zero-byte file is a valid SQLite database with no tables."""
    path, _ = database
    import os

    empty = str(os.path.join(os.path.dirname(path), "empty.db"))
    open(empty, "wb").close()
    opened = db_inspect.open_readonly(empty)
    try:
        text = db_inspect.describe(opened)
        assert "tables" in text
        assert db_inspect.query(opened, "SELECT 1 AS one").ok is True
    finally:
        opened.close()

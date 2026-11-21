"""Read-only inspection of the panel's own SQLite file — a map and a key.

Two primitives, and deliberately nothing more:

* :func:`describe` — what is in the file: tables, columns, types, primary keys,
  indexes, row counts, the actual values of enumeration columns, which columns
  hold credentials. This is the half that makes ``query`` usable: a model that
  does not know a column name cannot write a statement that runs.
* :func:`query` — run one read-only statement and render the result as a table.

Every summary (counts, failure rates, turn-time percentiles, orphan rows) is a
composition of ``query``. None of them live here. A prebuilt report can only
answer the questions its author thought of; a map plus a key answers the rest,
and leaves the arithmetic where the question is.

**The read-only guarantee is a property of the connection, not a promise in this
docstring.** ``mode=ro`` plus ``PRAGMA query_only`` make a write statement fail
*inside SQLite*, which is what lets both capabilities honestly claim the ``read``
tier. Everything above that — the statement head allow-list, the masking of
credential columns, the row budget — is defence in depth for the case where the
statement is well-formed but the answer is one nobody should see.

One WAL caveat, because getting it wrong produces a confident wrong answer:
opening a WAL database read-only still needs to create or map its ``-shm`` file.
If the directory is not writable that open fails, and it must **stay** failed —
falling back to ``immutable=1`` would ignore ``-wal`` and report the state of the
last checkpoint as if it were the present. Stale numbers that look like real
numbers are worse than an error.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

#: Columns that hold a credential. Masked on the way out by column name, so the
#: masking does not depend on how the statement was written — ``SELECT *`` leaks
#: no more than an explicit list.
SENSITIVE_COLUMNS = frozenset({"manage_token", "push_token"})

#: Statement heads that can read. ``pragma`` is here because ``table_info`` and
#: ``integrity_check`` are exactly the primitives a reader wants; the write-shaped
#: pragmas are refused by ``mode=ro`` itself, not by this list.
ALLOWED_HEADS = frozenset({"select", "with", "values", "explain", "pragma"})

#: Column names that are enumerations in practice. Used to decide which columns
#: are worth a ``GROUP BY`` in :func:`describe`; a guess, but a cheap and useful
#: one, and ``describe`` says where the values came from.
# Columns whose values are drawn from a small fixed set. `source` deliberately is
# not here: `session_attachments.source` stores the attachment URI, so grouping it
# scans every row to learn nothing.
ENUM_HINTS = ("state", "status", "role", "kind", "outcome", "mime", "type")

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
DEFAULT_BUDGET_MS = 3000

MAX_CELL_WIDTH = 60
MAX_TABLE_WIDTH = 220
MAX_ENUM_VALUES = 12

#: The catalog clips a result at 4000 characters. Render *inside* that budget
#: rather than relying on the clip: the clip cuts from the tail, and the tail is
#: where "showing N of at least M" lives — the one line that tells a caller it is
#: looking at a page rather than the whole answer.
RESULT_BUDGET_CHARS = 3400

#: How much row data may be materialised before the fetch stops. A row cap alone
#: bounds rows, not bytes, and one `SELECT * FROM session_attachments` can hold a
#: 5 MB image per row — 5000 of those is an out-of-memory, not a result. The
#: deadline is no defence either: it is checked every 2000 VM steps, and a single
#: `fetchmany` allocates far more than that between checks.
MAX_RESULT_BYTES = 8 * 1024 * 1024


class ReadOnlyError(RuntimeError):
    """The file could not be opened for reading; the reason is in the message."""


@dataclass(frozen=True)
class Answer:
    """What one ``query`` produced — the rendered table, and whether it ran."""

    text: str
    ok: bool
    elapsed_ms: int


# ------------------------------------------------------------------- connection


def open_readonly(path: str) -> sqlite3.Connection:
    """Open ``path`` for reading only, or raise :class:`ReadOnlyError`.

    The caller owns the connection and must close it. The returned connection is
    in autocommit mode with an explicit read transaction already open, so every
    statement inside one call sees the same snapshot instead of a file being
    written underneath it.
    """
    target = Path(path).expanduser()
    try:
        absolute = target.resolve(strict=True)
    except OSError as exc:
        raise ReadOnlyError(f"database not found: {target}") from exc
    if not absolute.is_file():
        raise ReadOnlyError(f"not a file: {absolute}")

    uri = f"{absolute.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise ReadOnlyError(
            f"cannot open {absolute} read-only: {exc}. "
            "A write-ahead-logged database still needs a writable directory for its "
            "-shm file; copy it somewhere writable, or stop the panel and checkpoint. "
            "Do not fall back to immutable=1 — it ignores the -wal and reports stale data."
        ) from exc

    connection.row_factory = sqlite3.Row
    connection.isolation_level = None
    try:
        connection.execute("PRAGMA query_only = 1")
        connection.execute("BEGIN")
        # `BEGIN` is deferred: it takes no snapshot and reads no page, so on its own
        # it proves nothing about a WAL file whose `-shm` cannot be created. Touch
        # the file here, or the failure surfaces later as a generic 500 inside a
        # query instead of the 503 with -shm advice this function promises.
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        connection.close()
        raise ReadOnlyError(
            f"opened {absolute} but could not start a read transaction: {exc}. "
            "A write-ahead-logged database needs a writable directory for its -shm file."
        ) from exc
    return connection


@contextmanager
def _deadline(connection: sqlite3.Connection, budget_ms: int) -> Iterator[None]:
    """Interrupt a statement that overruns ``budget_ms``.

    A recursive CTE or a cartesian join on a large file does not fail — it simply
    keeps going. Without this the page hangs waiting for an answer that is never
    coming, which is worse than an error the model can read and react to.
    """
    if not budget_ms or budget_ms <= 0:
        yield
        return
    ends_at = time.monotonic() + budget_ms / 1000.0

    def guard() -> int:
        return 1 if time.monotonic() > ends_at else 0

    connection.set_progress_handler(guard, 2000)
    try:
        yield
    finally:
        connection.set_progress_handler(None, 0)


# ---------------------------------------------------------------------- helpers


def _quote(name: str) -> str:
    """A table name safe to interpolate into a PRAGMA."""
    return '"' + str(name).replace('"', '""') + '"'


def _first_word(statement: str) -> str:
    """The leading keyword, skipping comments and whitespace."""
    text = statement.strip()
    while True:
        if text.startswith("--"):
            newline = text.find("\n")
            if newline < 0:
                return ""
            text = text[newline + 1 :].strip()
        elif text.startswith("/*"):
            end = text.find("*/")
            if end < 0:
                return ""
            text = text[end + 2 :].strip()
        else:
            break
    match = re.match(r"[A-Za-z]+", text)
    return match.group(0).lower() if match else ""


def _width(text: str) -> int:
    """Display width, counting East Asian wide characters as two columns."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _fit(text: str, width: int) -> str:
    """Cut ``text`` to ``width`` display columns, marking that it was cut."""
    if _width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        cell = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + cell > width - 1:
            break
        kept.append(char)
        used += cell
    return "".join(kept) + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _mask(value: Any) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()
    return f"sha256:{digest[:8]}"


def _cell(value: Any, *, mask: bool) -> str:
    if mask and value is not None:
        return _mask(value)
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {_size(len(bytes(value)))}>"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    if any(char in text for char in "\r\n\t"):
        text = re.sub(r"\s+", " ", text)
    return text


def _bytes_of(value: Any) -> int:
    """Roughly how much memory one fetched value holds."""
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return len(str(value))


def _table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render rows as an aligned table.

    Rows are value arrays against one header, because repeating the column names
    on every row would spend the result budget on the schema instead of the data.
    """
    heads = [str(column) for column in columns]
    widths: list[int] = []
    for index, head in enumerate(heads):
        width = min(MAX_CELL_WIDTH, max(_width(head), 1))
        for row in rows:
            width = max(width, min(MAX_CELL_WIDTH, _width(str(row[index]))))
        widths.append(width)

    total = sum(widths) + 2 * (len(widths) - 1) if widths else 0
    if total > MAX_TABLE_WIDTH and widths:
        share = max(4, (MAX_TABLE_WIDTH - 2 * (len(widths) - 1)) // len(widths))
        widths = [min(width, share) for width in widths]

    def render(cells: Sequence[str]) -> str:
        return "  ".join(_pad(_fit(str(cell), width), width) for cell, width in zip(cells, widths)).rstrip()

    lines = [render(heads)]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines)


def _tables(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Every user table mapped to its column names, in schema order."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: tuple(
            row[1] for row in connection.execute(f"PRAGMA table_info({_quote(name)})")
        )
        for name in names
    }


def _count(connection: sqlite3.Connection, table: str) -> int:
    try:
        return int(connection.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0])
    except sqlite3.Error as exc:
        # The deadline interrupt is not a per-table failure: swallowing it let a
        # timed-out describe keep going and answer `ok=True` with a partial map.
        if "interrupted" in str(exc).lower():
            raise
        return -1


# --------------------------------------------------------------------- describe


def describe(
    connection: sqlite3.Connection,
    table: str | None = None,
    *,
    budget_ms: int = DEFAULT_BUDGET_MS,
) -> str:
    """The map as text. See :func:`describe_answer` for the same thing with a status."""
    return describe_answer(connection, table, budget_ms=budget_ms).text


def describe_answer(
    connection: sqlite3.Connection,
    table: str | None = None,
    *,
    budget_ms: int = DEFAULT_BUDGET_MS,
) -> Answer:
    """What is in this file — all tables at a glance, or one table in full.

    Without ``table`` the answer is one line per table: name, row count, and the
    column names in order. That fits a result budget and is usually enough to
    start writing a statement. With ``table`` it adds types, primary keys,
    indexes, a sample value and — for columns that look like enumerations — the
    values actually present in the data, with counts.

    The enumeration values matter more than they look: they are read from the
    file, so a state this release has never heard of still shows up. A list
    hard-coded here would quietly stop being the truth the first time the schema
    grew.
    """
    try:
        known = _tables(connection)
    except sqlite3.Error as exc:
        return Answer(f"cannot read this database: {exc}", False, 0)
    if table:
        wanted = table.strip()
        if wanted not in known:
            close = difflib.get_close_matches(wanted, sorted(known), n=3, cutoff=0.4)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            return Answer(
                f"no such table: {wanted}{hint}\ntables: {', '.join(known) or '(none)'}",
                False,
                0,
            )
        try:
            with _deadline(connection, budget_ms):
                return Answer(_describe_table(connection, wanted), True, 0)
        except sqlite3.Error as exc:
            if "interrupted" in str(exc).lower():
                return Answer(
                    f"describe stopped after {budget_ms}ms — ask for one table at a time",
                    False,
                    0,
                )
            return Answer(f"cannot describe {wanted}: {exc}", False, 0)

    lines = ["tables (* = primary key, † = never readable; call panel_db_schema(table='…') for types, indexes and values)"]
    stopped: sqlite3.Error | None = None
    try:
        with _deadline(connection, budget_ms):
            for name, columns in known.items():
                keys = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({_quote(name)})")
                    if row[5]
                }
                marked = ", ".join(
                    column + ("*" if column in keys else "") + ("†" if column in SENSITIVE_COLUMNS else "")
                    for column in columns
                )
                if _width(marked) > 150:
                    marked = _fit(marked, 150)
                count = _count(connection, name)
                rows = f"{count} rows" if count >= 0 else "row count unavailable"
                lines.append(f"  {name}  ({rows}): {marked}")
    except sqlite3.Error as exc:
        stopped = exc
        lines.append(f"  (stopped early: {exc})")

    lines.append(
        '  note: at / created_at / updated_at / checked_at are fixed-width UTC strings '
        '("YYYY-MM-DDTHH:MM:SSZ"), so text order is time order and substr(col,1,13) is the hour.'
    )
    return Answer(_cap("\n".join(lines)), stopped is None, 0)


def _describe_table(connection: sqlite3.Connection, table: str) -> str:
    quoted = _quote(table)
    info = list(connection.execute(f"PRAGMA table_info({quoted})"))
    counted = _count(connection, table)
    lines = [f"{table}  {counted} rows" if counted >= 0 else f"{table}  row count unavailable"]

    for row in info:
        name = row[1]
        declared = (row[2] or "").strip()
        flags = []
        if row[5]:
            flags.append("pk")
        if row[3]:
            flags.append("not null")
        if name in SENSITIVE_COLUMNS:
            flags.append("†")
        head = f"  {name:<18} {(declared or '-'):<8}".rstrip()
        if flags:
            head = f"{head}  [{' '.join(flags)}]"
        lines.append(head)

        values = _enum_values(connection, quoted, name) if _is_enum(name) else None
        if values is not None:
            lines.append(f"      values: {values}")
        elif not _is_blob(declared):
            # Not an enum, or too many values to list — a sample still helps.
            sample = _sample(connection, quoted, name)
            if sample is not None:
                lines.append(f"      e.g. {sample}")

    indexes = []
    for row in connection.execute(f"PRAGMA index_list({quoted})"):
        name = str(row[1])
        parts = [str(part[2]) for part in connection.execute(f"PRAGMA index_info({_quote(name)})")]
        indexes.append(f"{name} ({', '.join(parts)})")
    lines.append(f"  indexes: {'; '.join(indexes) if indexes else '(none)'}")
    return "\n".join(lines)


def _cap(text: str) -> str:
    """Keep a describe inside the result budget, and say what was cut."""
    if len(text) <= RESULT_BUDGET_CHARS:
        return text
    note = "\n(… describe was cut to fit; ask panel_db_schema(table='…') for one table in full)"
    return text[: RESULT_BUDGET_CHARS - len(note)] + note


def _is_enum(column: str) -> bool:
    lowered = column.lower()
    return any(hint in lowered for hint in ENUM_HINTS)


def _is_blob(declared: str) -> bool:
    return "BLOB" in declared.upper() or declared == ""


def _enum_values(
    connection: sqlite3.Connection, quoted_table: str, column: str
) -> str | None:
    """The distinct values a column holds, when there are few enough to list."""
    try:
        rows = list(
            connection.execute(
                f"SELECT {_quote(column)}, count(*) FROM {quoted_table} "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT {MAX_ENUM_VALUES + 1}"
            )
        )
    except sqlite3.Error as exc:
        if "interrupted" in str(exc).lower():
            raise
        return None
    if not rows or len(rows) > MAX_ENUM_VALUES:
        return None
    if len(rows) == 1 and rows[0][0] is None:
        return None
    return ", ".join(
        f"{_fit(_cell(value, mask=column in SENSITIVE_COLUMNS), MAX_CELL_WIDTH)}={count}"
        for value, count in rows
    )


def _sample(
    connection: sqlite3.Connection, quoted_table: str, column: str
) -> str | None:
    try:
        row = connection.execute(
            f"SELECT {_quote(column)} FROM {quoted_table} "
            f"WHERE {_quote(column)} IS NOT NULL LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        if "interrupted" in str(exc).lower():
            raise
        return None
    if row is None:
        return None
    text = _cell(row[0], mask=column in SENSITIVE_COLUMNS)
    return _fit(text.replace("\n", " "), 60)


# ------------------------------------------------------------------------ query


def query(
    connection: sqlite3.Connection,
    sql: str,
    *,
    limit: int = DEFAULT_LIMIT,
    budget_ms: int = DEFAULT_BUDGET_MS,
) -> Answer:
    """Run one read-only statement and render it as a table.

    The result is text rather than JSON on purpose: the same character budget
    holds roughly three times as many rows, and a table is something the answer
    can quote back at a person without reformatting it.

    A failure names itself and, where it can, says what to do next. That is not
    politeness — a model that cannot tell a typo from an empty table burns its
    whole step budget guessing column names, and then reports that the data was
    never there.
    """
    statement = str(sql or "").strip()
    started = time.monotonic()
    if not statement:
        return Answer(
            f"SQL_ERROR: empty statement (this connection runs {' / '.join(sorted(ALLOWED_HEADS))})",
            False,
            0,
        )

    head = _first_word(statement)
    if head not in ALLOWED_HEADS:
        return Answer(
            f"SQL_ERROR: {head.upper() or 'that'} is not read-only. "
            f"This connection runs {' / '.join(sorted(ALLOWED_HEADS))} only.",
            False,
            0,
        )

    wanted = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    # Which credential columns does this statement actually touch? Ask SQLite, not
    # the projection: `SELECT push_token AS t` and `substr(push_token,1,20)` both
    # keep their secret and neither keeps the column's name, so a mask keyed off
    # `cursor.description` would leak the one thing it exists to hide. The
    # authorizer fires once per referenced column while the statement is being
    # prepared, which is the only place that information exists.
    secrets_read: set[str] = set()

    def _authorizer(action: int, _arg1: object, arg2: object, _db: object, _trigger: object) -> int:
        if action == sqlite3.SQLITE_READ and arg2 is not None:
            name = str(arg2).lower()
            if name in SENSITIVE_COLUMNS:
                secrets_read.add(name)
                # Decide, do not redact. Masking the cell still leaves the row count,
                # the ordering and the success/failure of a WHERE clause visible —
                # an oracle that recovers a token one comparison at a time. Refusing
                # the read closes all of those at once, and it is the only thing the
                # ``read`` tier can honestly promise about a credential column.
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        with _deadline(connection, budget_ms):
            connection.set_authorizer(_authorizer)
            try:
                cursor = connection.execute(statement)
            finally:
                connection.set_authorizer(None)
            columns = [str(item[0]) for item in (cursor.description or [])]
            if not columns:
                return Answer(
                    "sql ok  0 rows  (the statement produced no result set)",
                    True,
                    _elapsed(started),
                )

            fetched: list[sqlite3.Row] = []
            held = 0
            byte_capped = False
            oversized = False
            while len(fetched) <= wanted:
                # One row per call: the budget is checked per row, and a batch is
                # allocated in full before any check can run.
                batch = cursor.fetchmany(1)
                if not batch:
                    break
                stop = False
                for row in batch:
                    size = sum(_bytes_of(value) for value in row)
                    if held + size > MAX_RESULT_BYTES:
                        stop = True
                        byte_capped = True
                        # Nothing retained: the very first row is already over the
                        # limit. Saying "no rows matched" here would be a lie about
                        # the data rather than about the budget.
                        oversized = not fetched
                        break
                    held += size
                    fetched.append(row)
                if stop:
                    break
    except (sqlite3.Error, sqlite3.Warning) as exc:
        # ``sqlite3.Warning`` is not a ``sqlite3.Error``: a multi-statement string
        # raises it, and letting it escape turns a user mistake into a 500.
        message = str(exc)
        if "interrupted" in message.lower():
            return Answer(
                f"SQL_TIMEOUT: over {budget_ms}ms — narrow the window with WHERE, "
                "or aggregate with GROUP BY instead of listing rows.",
                False,
                _elapsed(started),
            )
        if secrets_read:
            return Answer(
                f"SQL_ERROR: this statement reads a credential column "
                f"({', '.join(sorted(secrets_read))}), which is refused rather than "
                "masked — selecting, filtering or ordering by it would still leak it "
                "through the row count. Project the other columns instead "
                '(panel_db_schema lists them).',
                False,
                _elapsed(started),
            )
        if "one statement at a time" in message.lower():
            return Answer(
                "SQL_ERROR: run one statement at a time — this endpoint takes a single "
                "SELECT / WITH / PRAGMA, and does not split on ';'.",
                False,
                _elapsed(started),
            )
        return Answer(_failure(connection, message), False, _elapsed(started))

    # Nothing to mask: the authorizer refused any statement that touches a
    # credential column, so every value here came from a column that is safe to
    # show. (`describe` still masks the sample value it reads directly.)
    rendered = [[_cell(value, mask=False) for value in row] for row in fetched[:wanted]]

    # The highest count we can honestly report without scanning the table: the
    # rows in hand. An exact total is a `SELECT count(*)` away, which is the
    # caller's job to ask for — and the only way to get it without holding every
    # BLOB in memory, which is what this loop exists to avoid.
    available = len(fetched)
    more = available > wanted
    elapsed = _elapsed(started)
    empty_note = (
        f"the first row alone exceeds the {MAX_RESULT_BYTES // (1024 * 1024)} MB size budget — "
        "select fewer columns (a BLOB can be most of a row)"
        if oversized
        else "the statement ran and matched no rows — that is an empty result, not an error"
    )

    def render(rows: list[list[str]], cut: bool = False) -> str:
        shown = len(rows)
        count = "row" if shown == 1 else "rows"
        head = f"sql ok  {shown} {count}"
        if shown < available:
            head += f" of {'at least ' if more else ''}{available}"
        head += f"  {elapsed}ms"
        parts = [head, _table(columns, rows) if rows else empty_note]
        if cut:
            limit = "the size budget" if byte_capped else "the answer budget"
            parts.append(
                f"(rendered {shown} of {'at least ' if more else ''}{available} rows to stay "
                f"inside {limit} — ask for fewer columns, or aggregate)"
            )
        elif byte_capped:
            # Distinct from `more`: the fetch stopped on size, so we do not even
            # know how many rows exist. Saying "showing 8" without this would read
            # as "there are 8".
            parts.append(
                f"(stopped at the {MAX_RESULT_BYTES // (1024 * 1024)} MB size budget after "
                f"{shown} rows — select fewer columns, or aggregate; count(*) gives the total)"
            )
        elif more:
            parts.append(
                f"(showing {shown}, and there are more rows — narrow with WHERE, or "
                "aggregate with GROUP BY; count(*) returns the total)"
            )
        return "\n".join(parts)

    body = render(rendered)
    if len(body) > RESULT_BUDGET_CHARS and len(rendered) > 1:
        # Fit the budget by showing fewer rows, never by cutting the text: the
        # notes above are the part a caller cannot reconstruct.
        low, high, best = 1, len(rendered), render(rendered[:1])
        while low <= high:
            middle = (low + high) // 2
            candidate = render(rendered[:middle], cut=True)
            if len(candidate) <= RESULT_BUDGET_CHARS:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        body = best
    return Answer(body, True, elapsed)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _failure(connection: sqlite3.Connection, message: str) -> str:
    """``SQL_ERROR`` plus, when we can tell, the name that was probably meant."""
    head = f"SQL_ERROR: {message}"
    match = re.search(r"no such (column|table):\s*(\S+)", message, re.IGNORECASE)
    if not match:
        return head
    kind, missing = match.group(1).lower(), match.group(2).strip()

    try:
        known = _tables(connection)
    except sqlite3.Error:
        return head

    if kind == "table":
        close = difflib.get_close_matches(missing, sorted(known), n=3, cutoff=0.4)
        if close:
            return f"{head}\nhint: did you mean {', '.join(close)}? panel_db_schema() lists every table."
        return f"{head}\nhint: panel_db_schema() lists every table in this file."

    owners: dict[str, list[str]] = {}
    for name, columns in known.items():
        for column in columns:
            owners.setdefault(column, []).append(name)
    close = difflib.get_close_matches(missing, sorted(owners), n=3, cutoff=0.4)
    if not close:
        return f"{head}\nhint: panel_db_schema(table='…') lists every column in the file."
    listed = ", ".join(f"{column} ({'/'.join(owners[column])})" for column in close)
    return f"{head}\nhint: did you mean {listed}? panel_db_schema(table='…') lists the rest."

"""Small shared helpers: UTC timestamps, JSON encoding, opaque identifiers."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    """Format a datetime as the fixed-width, second-precision UTC form ADP uses.

    ``2026-01-31T08:15:00Z`` — fixed width is what makes lexicographic ordering
    in SQLite agree with instant ordering.
    """
    moment = (value or utcnow()).astimezone(timezone.utc)
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(text: str) -> datetime:
    """Parse an RFC 3339 date-time into an aware UTC datetime.

    Raises:
        ValueError: if the value is not a valid date-time.
    """
    if not isinstance(text, str):
        raise ValueError("timestamp must be a string")
    value = text.strip()
    if not value:
        raise ValueError("timestamp must not be empty")
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def normalize_instant(text: str) -> str:
    """Parse any valid date-time and re-emit it in ADP's fixed-width form."""
    return iso(parse_instant(text))


def json_dumps(value: Any, *, pretty: bool = False) -> str:
    """Serialize to JSON without escaping non-ASCII characters."""
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(text: str | None, default: Any = None) -> Any:
    """Parse JSON, returning ``default`` when the text is empty or invalid."""
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def sha256_hex(text: str) -> str:
    """Lower-case hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_token(nbytes: int = 32) -> str:
    """A URL-safe random token (used for manage tokens and panel access)."""
    return secrets.token_urlsafe(nbytes)


def new_id(prefix: str = "t") -> str:
    """A short, collision-resistant identifier for local task records."""
    return f"{prefix}_{secrets.token_hex(8)}"


def truncate(text: str | None, limit: int = 200) -> str:
    """Collapse whitespace and cut a string to ``limit`` characters."""
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def title_from(text: str, limit: int = 60) -> str:
    """A one-line human title for a task, derived from its prompt."""
    return truncate(text, limit) or "(empty prompt)"

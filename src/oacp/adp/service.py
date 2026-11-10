"""The ADP Core registry: registration, search, deletion and probing.

The registry makes exactly one claim about an agent — *I fetched this card from
that URL at ``lastOkAt``* — and every branch below is written so that claim stays
true: failures never move ``lastOkAt``, a changed card is followed instead of
being judged, and no field is invented that would imply trust.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .. import net
from ..config import Settings
from ..db import Database
from ..util import (
    iso,
    json_dumps,
    json_loads,
    new_token,
    normalize_instant,
    sha256_hex,
    utcnow,
)
from . import card as cardmod
from .fetcher import FetchError, GuardedFetcher
from .jcs import card_hash

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class RegistryError(Exception):
    """An error that maps onto one of the contract's error responses."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class Registration:
    """Outcome of ``POST /v1/agents``."""

    record: dict[str, Any]
    manage_token: str | None
    created: bool


class RegistryService:
    """All five ADP operations plus the probe bookkeeping."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        fetcher: GuardedFetcher | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.fetcher = fetcher or GuardedFetcher(
            max_bytes=settings.max_card_bytes,
            user_agent=settings.user_agent,
            allow_private=settings.allow_insecure_cards,
        )

    # ------------------------------------------------------------------ writing

    def register(self, url: str, *, client_ip: str = "unknown") -> Registration:
        """Fetch, validate and store an agent, idempotently.

        The id is derived from the normalised URL (ADP §4.1), so a repeat
        registration is answerable from storage alone: the stored record comes
        back untouched and the manage token is never rotated.
        """
        try:
            normalized = net.normalize_url(url, allow_http=self.settings.allow_insecure_cards)
        except net.UrlError as exc:
            raise RegistryError(400, "BAD_URL", str(exc)) from exc

        ip = client_ip or "unknown"
        self._assert_ip_budget(ip)
        host = net.host_of(normalized)
        agent_id = net.derive_id(normalized)

        existing = self._load(agent_id)
        if existing is not None:
            with self.db.connect() as connection:
                self._record_registration(connection, ip, host, iso(), "existing")
            return Registration(record=self._record(existing), manage_token=None, created=False)

        document = self._fetch_card(normalized)
        self._assert_card(document.payload)
        now = iso()
        token = new_token()
        try:
            with self.db.connect() as connection:
                self._assert_domain_budget(connection, host, agent_id)
                connection.execute(
                    """
                    INSERT INTO agents (
                        id, url, host, card, card_hash, status, first_seen_at,
                        checked_at, last_ok_at, failures, changed_at, prev_hash, manage_token
                    ) VALUES (?, ?, ?, ?, ?, 'ok', ?, ?, ?, 0, NULL, NULL, ?)
                    """,
                    (
                        agent_id,
                        normalized,
                        host,
                        json_dumps(document.payload),
                        card_hash(document.payload),
                        now,
                        now,
                        now,
                        sha256_hex(token),
                    ),
                )
                self._record_registration(connection, ip, host, now, "created")
                row = connection.execute(
                    "SELECT * FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
        except sqlite3.IntegrityError:
            # A concurrent request registered the same URL first; the id is
            # derived from the URL, so both callers mean the same entry.
            row = self._load(agent_id)
            if row is None:
                raise
            with self.db.connect() as connection:
                self._record_registration(connection, ip, host, iso(), "existing")
            return Registration(record=self._record(row), manage_token=None, created=False)

        return Registration(record=self._record(row), manage_token=token, created=True)

    def delete(self, agent_id: str, token: str | None) -> None:
        """Hard-delete an entry; only the first registrant's token is accepted."""
        if not token:
            raise RegistryError(401, "UNAUTHORIZED", "X-ADP-Token header is required")
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None:
                raise RegistryError(404, "NOT_FOUND", f"no agent with id {agent_id}")
            if sha256_hex(token) != row["manage_token"]:
                raise RegistryError(401, "UNAUTHORIZED", "manage token does not match")
            connection.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def force_delete(self, agent_id: str, *, client_ip: str = "unknown") -> dict[str, Any]:
        """Delete an entry without asking for its manage token; return the record.

        The contract's rule (``DELETE /v1/agents/{id}``) is that only the holder
        of the manage token may remove an entry, and :meth:`delete` keeps exactly
        that. An operator, however, has to be able to clear out entries nobody
        can prove ownership of — a self-registered agent, or one registered from
        somebody else's browser — or the registry becomes append-only by accident.

        So the *authorisation* question is answered one layer up (the panel
        decides who counts as an operator, see ``api._operator``) and this method
        only performs the removal. Two things it does keep: the removed record is
        returned, and the removal is written to the registration log with
        ``outcome="removed-by-operator"``, so the audit trail has the same shape
        as registrations instead of being a hole.
        """
        removed: dict[str, Any]
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                raise RegistryError(404, "NOT_FOUND", f"no agent with id {agent_id}")
            removed = self._record(row)
            connection.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            self._record_registration(
                connection, client_ip or "unknown", row["host"], iso(), "removed-by-operator"
            )
        return removed

    def probe(self, agent_id: str) -> dict[str, Any] | None:
        """Fetch one agent's card now and apply the freshness rules."""
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return self._probe_row(row)

    def _probe_row(self, row: sqlite3.Row) -> dict[str, Any] | None:
        agent_id = row["id"]
        checked = iso()
        try:
            document = self._fetch_card(row["url"])
            self._assert_card(document.payload)
        except (FetchError, RegistryError):
            self._mark_failure(agent_id, checked)
            return self.get(agent_id)

        digest = card_hash(document.payload)
        with self.db.connect() as connection:
            if digest == row["card_hash"]:
                connection.execute(
                    """
                    UPDATE agents
                       SET checked_at = ?, last_ok_at = ?, failures = 0, status = 'ok'
                     WHERE id = ?
                    """,
                    (checked, checked, agent_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE agents
                       SET card = ?, card_hash = ?, prev_hash = ?, changed_at = ?,
                           checked_at = ?, last_ok_at = ?, failures = 0, status = 'ok'
                     WHERE id = ?
                    """,
                    (
                        json_dumps(document.payload),
                        digest,
                        row["card_hash"],
                        checked,
                        checked,
                        checked,
                        agent_id,
                    ),
                )
        return self.get(agent_id)

    def _mark_failure(self, agent_id: str, checked: str) -> None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT failures FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return
            failures = int(row["failures"]) + 1
            status = "down" if failures >= 3 else "ok"
            connection.execute(
                "UPDATE agents SET checked_at = ?, failures = ?, status = ? WHERE id = ?",
                (checked, failures, status, agent_id),
            )

    # ------------------------------------------------------------------ reading

    def get(self, agent_id: str) -> dict[str, Any] | None:
        """One full AgentRecord, or None."""
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return self._record(row) if row is not None else None

    def list_agents(
        self,
        *,
        q: str | None = None,
        skill: str | None = None,
        tag: str | None = None,
        status: str | None = None,
        changed_since: str | None = None,
        full: bool = False,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search agents; results are ordered by ``firstSeenAt`` desc, ``id`` asc."""
        if status is not None and status not in {"ok", "down"}:
            raise RegistryError(400, "BAD_REQUEST", "status must be 'ok' or 'down'")
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise RegistryError(400, "BAD_REQUEST", f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if q is not None and len(q) > 200:
            raise RegistryError(400, "BAD_REQUEST", "q must be at most 200 characters")

        since = None
        if changed_since:
            try:
                since = normalize_instant(changed_since)
            except ValueError as exc:
                raise RegistryError(400, "BAD_REQUEST", f"changedSince is not a date-time: {exc}") from exc

        sql = "SELECT * FROM agents"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since:
            clauses.append("changed_at IS NOT NULL AND changed_at >= ?")
            params.append(since)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY first_seen_at DESC, id ASC"

        with self.db.connect() as connection:
            rows = connection.execute(sql, params).fetchall()

        needle = (q or "").casefold().strip()
        filtered = [row for row in rows if self._matches(row, needle, skill, tag)]

        start = 0
        if cursor:
            start = _cursor_index(filtered, cursor)

        page = filtered[start : start + limit]
        next_cursor = None
        if start + limit < len(filtered):
            last = page[-1] if page else None
            if last is not None:
                next_cursor = _encode_cursor(last["first_seen_at"], last["id"])

        items = [
            self._record(row, full=full) if full else self._summary(row)
            for row in page
        ]
        return {"items": items, "nextCursor": next_cursor}

    def _matches(
        self,
        row: sqlite3.Row,
        needle: str,
        skill: str | None,
        tag: str | None,
    ) -> bool:
        card = json_loads(row["card"], {})
        summary = cardmod.card_summary(card)
        if needle:
            haystack = [summary["name"], summary["description"]]
            skills = card.get("skills") if isinstance(card.get("skills"), list) else []
            for item in skills:
                if isinstance(item, dict):
                    haystack.append(str(item.get("name") or ""))
                    haystack.append(str(item.get("description") or ""))
            if not any(needle in value.casefold() for value in haystack):
                return False
        if skill and skill not in summary["skills"]:
            return False
        if tag and tag not in summary["tags"]:
            return False
        return True

    # ------------------------------------------------------------------ probing

    def due_agents(self, limit: int) -> list[dict[str, Any]]:
        """Agents whose probe interval has elapsed, oldest check first."""
        now = utcnow()
        ok_before = iso(now - timedelta(seconds=self.settings.probe_interval))
        down_before = iso(now - timedelta(seconds=self.settings.down_probe_interval))
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agents
                 WHERE (status = 'ok' AND checked_at <= ?)
                    OR (status = 'down' AND checked_at <= ?)
                 ORDER BY checked_at ASC
                 LIMIT ?
                """,
                (ok_before, down_before, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def probe_due(self, limit: int = 4) -> int:
        """Probe up to ``limit`` due agents; returns how many were probed."""
        rows = self.due_agents(limit)
        for row in rows:
            self._probe_row(row)
        return len(rows)

    # ------------------------------------------------------------------ helpers

    def _load(self, agent_id: str) -> sqlite3.Row | None:
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()

    def _fetch_card(self, url: str) -> Any:
        try:
            return self.fetcher.fetch(url)
        except FetchError as exc:
            raise RegistryError(422, exc.code, exc.message, exc.detail) from exc

    def _assert_card(self, payload: Any) -> None:
        mode = self.settings.card_validation
        problems = cardmod.validate_card(payload, mode=mode)
        if problems:
            raise RegistryError(
                422,
                "NOT_AN_AGENT_CARD",
                "the document is not a valid AgentCard",
                {"problems": problems, "mode": mode},
            )

    def _assert_ip_budget(self, ip: str) -> None:
        cutoff = iso(utcnow() - timedelta(hours=1))
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM registrations WHERE ip = ? AND at >= ?",
                (ip, cutoff),
            ).fetchone()["n"]
        if count >= self.settings.register_per_hour:
            raise RegistryError(
                429,
                "RATE_LIMITED",
                "too many registrations from this address",
                {"retryAfterSeconds": 3600},
                {"Retry-After": "3600"},
            )

    def _assert_domain_budget(
        self,
        connection: sqlite3.Connection,
        host: str,
        agent_id: str,
    ) -> None:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE host = ? AND id != ?",
            (host, agent_id),
        ).fetchone()["n"]
        if count >= self.settings.max_agents_per_domain:
            raise RegistryError(
                429,
                "RATE_LIMITED",
                "this host already has the maximum number of agents",
                {"maxAgentsPerDomain": self.settings.max_agents_per_domain},
            )

    def _record_registration(
        self,
        connection: sqlite3.Connection,
        ip: str,
        host: str,
        at: str,
        outcome: str,
    ) -> None:
        connection.execute(
            "INSERT INTO registrations (ip, host, at, outcome) VALUES (?, ?, ?, ?)",
            (ip, host, at, outcome),
        )

    def _record(self, row: sqlite3.Row, *, full: bool = True) -> dict[str, Any]:
        """One record in exactly the shape the contract publishes.

        The two views are disjoint, not nested. ``AgentRecord`` carries ``card``
        and the probe history but no facets; ``AgentSummary`` carries the facets
        and no card. Both are closed objects (``additionalProperties: false``), so
        a key that only "would be handy" makes the response non-conformant — the
        panel adds what it needs on its own surface instead.
        """
        card = json_loads(row["card"], {})
        base = {
            "id": row["id"],
            "url": row["url"],
            "status": row["status"],
            "cardHash": row["card_hash"],
            "checkedAt": row["checked_at"],
            "lastOkAt": row["last_ok_at"],
            "failures": int(row["failures"]),
        }
        if not full:
            summary = cardmod.card_summary(card)
            return {
                **base,
                "name": summary["name"],
                "description": summary["description"],
                "skills": summary["skills"],
                "tags": summary["tags"],
            }
        return {
            **base,
            "card": card,
            "firstSeenAt": row["first_seen_at"],
            "changedAt": row["changed_at"],
            "prevHash": row["prev_hash"],
        }

    def _summary(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._record(row, full=False)


# ------------------------------------------------------------------ cursors


def _encode_cursor(first_seen_at: str, agent_id: str) -> str:
    payload = json_dumps({"f": first_seen_at, "i": agent_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_index(rows: list[sqlite3.Row], cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        key_first_seen = str(data["f"])
        key_id = str(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RegistryError(400, "BAD_REQUEST", "cursor is not valid") from exc

    for index, row in enumerate(rows):
        if row["first_seen_at"] == key_first_seen and row["id"] == key_id:
            return index + 1
    # The row the cursor pointed at is gone (deleted between pages): restart is
    # safer than silently skipping entries.
    raise RegistryError(400, "BAD_REQUEST", "cursor no longer matches any entry")

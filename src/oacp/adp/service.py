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
        """Fetch, validate and store an agent, keyed on ``(host, card name)``.

        The id no longer comes from the URL, so the card has to be fetched *before*
        the entry can be located: a repeat registration now costs one round trip
        instead of zero. That is the price of the key, paid on purpose — the id is
        what every conversation points at, and it has to survive a restart on a new
        port. An agent that is offline can therefore no longer confirm a repeat
        registration; it fails the fetch like any other unreachable URL.

        Four outcomes, and the difference between the last three is the whole point:

        * ``(host, name)`` is new -> 201, a fresh entry with a manage token;
        * it exists under the **same** url -> 200, storage untouched (a repeat
          registration must not rotate the token or move any timestamp);
        * it exists under a **different** url -> 200, the entry **moves**: url, card
          and hashes are replaced while the id, ``first_seen_at`` and the manage
          token stay. This is what makes a port change cost nothing;
        * the url is already held by a **different** id -> 200, the entry
          **adopts** the new card: the card is replaced while the id,
          ``first_seen_at`` and the manage token stay. The id keeps spelling the
          old name's digest, because a handle is stable within an entry.
        """
        try:
            normalized = net.normalize_url(url, allow_http=self.settings.allow_insecure_cards)
        except net.UrlError as exc:
            raise RegistryError(400, "BAD_URL", str(exc)) from exc

        ip = client_ip or "unknown"
        self._assert_ip_budget(ip)
        host = net.host_of(normalized)

        document = self._fetch_card(normalized)
        self._assert_card(document.payload)
        name = cardmod.card_summary(document.payload)["name"]
        agent_id = net.derive_id(host, net.path_of(normalized), name)

        existing = self._load(agent_id)
        if existing is not None:
            if existing["url"] == normalized:
                # The id matches and so does the address — but that proves the key is
                # the same, not that the *stored card is current*. A card renamed
                # A -> B -> A derives this entry's original id again, and treating the
                # match as proof would answer 200 with the stale "B" card while the
                # caller holds the "A" document it just fetched and validated.
                # ``_adopt_name`` follows it: an unchanged digest records "existing"
                # and moves no timestamp, a changed one replaces the card.
                return self._adopt_name(existing, document, ip)
            return self._move(existing, normalized, document, ip)

        # A renamed card at a stable address: the url has always belonged to this
        # entry, and the id it now derives is consequently new. Looking the url up
        # here keeps the ordinary case off the IntegrityError path below — a renamed
        # entry would otherwise re-enter it on every single repeat registration.
        renamed = self._load_by_url(normalized)
        if renamed is not None:
            return self._adopt_name(renamed, document, ip)

        # An entry that was renamed keeps the id it was born with, so neither lookup
        # above finds it once it also changes address. Searching by the name it stored
        # last is the only thing that does — and without it the agent splits in two: a
        # dead entry under the old id and a live one under the new, with the history
        # stranded on the first. That is the exact failure this change exists to
        # remove, arriving by another door.
        adopted = self._load_by_name(host, net.path_of(normalized), name)
        if adopted is not None:
            return self._move(adopted, normalized, document, ip)

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
            # Either a concurrent request inserted this (host, name) first, or the
            # url belongs to an existing entry whose card has since been renamed.
            # ``agents.url`` is UNIQUE, so the second case surfaces here and not one
            # line earlier — and it must be resolved, not raised: the address is the
            # same agent, only its name (and therefore its id) changed.
            row = self._load(agent_id)
            if row is not None:
                if row["url"] != normalized:
                    return self._move(row, normalized, document, ip)
                return self._adopt_name(row, document, ip)
            renamed = self._load_by_url(normalized)
            if renamed is None:
                raise
            return self._adopt_name(renamed, document, ip)

        return Registration(record=self._record(row), manage_token=token, created=True)

    def _move(
        self,
        row: sqlite3.Row,
        normalized: str,
        document: Any,
        ip: str,
    ) -> Registration:
        """Point an existing entry at a new url — the port changed, the agent did not.

        The id, ``first_seen_at`` and the manage token are kept: the entry is the same
        one, and its conversations keep pointing at it. ``changed_at`` moves only when
        the card actually changed — a restart on a new port usually serves
        byte-identical bytes, and reporting "changed from X to X" would put the entry
        into every ``changedSince`` result.

        What this gives up is address ownership *within one host*, **across ports and
        paths**: a party able to serve a card with the same name from the same hostname
        can repoint an existing entry. That is the deliberate price of making a port
        change harmless, and it is why the key stops at the host — a shared or
        multi-tenant hostname wants ports back in the key.

        One condition narrows it to almost nothing: the entry's **current** url must
        have stopped answering. A restart on another port leaves the old port dead,
        which is exactly the case this rule exists for; a neighbour serving a same-name
        card from the same host leaves the old address alive, and is refused. The cost
        is one probe, on this path only.

        One thing this path must *not* do is trust the id on its own. An id match
        says the derivation produced the same digest, and a rename or a move keeps
        both host and path, so the (host, path) behind the stored row has to be the
        one just derived. A digest collision is the only way it is not — and
        answering one with a move would hand this entry's id, manage token and
        conversations to whoever registered the colliding address (§10: never
        merge).
        """
        host = net.host_of(normalized)
        if net.host_of(row["url"]) != host or net.path_of(row["url"]) != net.path_of(
            normalized
        ):
            raise RegistryError(
                500,
                "ID_COLLISION",
                f"the id for {normalized!r} is already held by {row['url']!r}",
            )

        holder = self._load_by_url(normalized)
        if holder is not None and holder["id"] != row["id"]:
            # Two entries on one host now claim the same name and the same address.
            # Refusing is the only honest answer: silently repointing would drop the
            # row that already owns the url (``agents.url`` is UNIQUE), and merging
            # would destroy a real entry (§10).
            raise RegistryError(
                409,
                "URL_TAKEN",
                f"{normalized!r} already belongs to another entry ({holder['id']})",
            )

        if self._still_serving(row):
            raise RegistryError(
                409,
                "AGENT_STILL_AT_OLD_URL",
                f"{row['url']!r} still serves a card, so this is a second agent with the "
                f"same name rather than a move; delete that entry before registering "
                f"{normalized!r}",
            )

        now = iso()
        digest = card_hash(document.payload)
        with self.db.connect() as connection:
            # Only the UPDATE is wrapped: it is the one statement whose failure means
            # "somebody else owns this url". A ``registrations`` constraint error, if one
            # is ever added, must not surface as a misleading URL_TAKEN.
            try:
                if digest == row["card_hash"]:
                    connection.execute(
                        """
                        UPDATE agents
                           SET url = ?, host = ?, checked_at = ?, last_ok_at = ?,
                               failures = 0, status = 'ok'
                         WHERE id = ?
                        """,
                        (normalized, host, now, now, row["id"]),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE agents
                           SET url = ?, host = ?, card = ?, card_hash = ?, prev_hash = ?,
                               changed_at = ?, checked_at = ?, last_ok_at = ?,
                               failures = 0, status = 'ok'
                         WHERE id = ?
                        """,
                        (
                            normalized,
                            host,
                            json_dumps(document.payload),
                            digest,
                            row["card_hash"],
                            now,
                            now,
                            now,
                            row["id"],
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                # The lookups above and this UPDATE run on different connections, so a
                # concurrent registration can take the url in between. ``agents.url`` is
                # UNIQUE and is the only constraint this statement can violate, so the
                # race means precisely what the explicit check above says.
                raise RegistryError(
                    409, "URL_TAKEN", f"{normalized!r} is already taken"
                ) from exc

            fresh = connection.execute(
                "SELECT * FROM agents WHERE id = ?", (row["id"],)
            ).fetchone()
            if fresh is None:
                # A concurrent delete removed the entry between the UPDATE and this read.
                # Nothing autocommits a transaction around the two, so it is possible.
                raise RegistryError(404, "NOT_FOUND", f"no agent with id {row['id']}")
            if fresh["url"] != normalized:
                # A concurrent registration moved this same entry to another address
                # between our lookups and this write. Answering 200 would tell the
                # caller its url was registered when another one won.
                raise RegistryError(409, "URL_TAKEN", f"{normalized!r} is already taken")
            self._record_registration(connection, ip, host, now, "moved")
        return Registration(record=self._record(fresh), manage_token=None, created=False)

    def _still_serving(self, row: sqlite3.Row) -> bool:
        """Whether *this entry's own agent* still answers at its current url.

        Not "does the address answer": a shared host can serve anything there, and an
        unrelated JSON endpoint would otherwise block a legitimate move with advice to
        delete the entry — the one action this rule exists to avoid. Only a card whose
        folded name matches what this entry stored counts as still being there.

        A failed fetch is the ordinary answer here, not an error: the port the agent
        used to be on is gone, which is precisely the case a move is for.
        """
        try:
            document = self._fetch_card(row["url"])
        except (FetchError, RegistryError):
            return False
        try:
            stored = json.loads(row["card"])
        except (TypeError, ValueError):
            return False
        if not isinstance(stored, dict) or not isinstance(document.payload, dict):
            return False
        served = cardmod.card_summary(document.payload)["name"]
        previous = cardmod.card_summary(stored)["name"]
        return net.name_key(served) == net.name_key(previous)

    def _adopt_name(
        self,
        row: sqlite3.Row,
        document: Any,
        ip: str,
    ) -> Registration:
        """A stable address whose card was renamed: keep the entry, take the new card.

        The url already belongs to this entry, so the agent did not move — it only
        changed what it calls itself. The derived id for the new name is a *different*
        id, and it is deliberately not adopted here: an id is a handle that stays
        stable within an entry (§4.1), not a function of the current content.
        Re-keying would re-point every conversation for a cosmetic edit, which is
        exactly the fragility this whole change exists to remove.

        Two consequences, both visible and intended: after a rename the id still
        spells the old name's digest, and a *second* entry for the new name cannot
        appear at this address, because ``agents.url`` is UNIQUE.
        """
        now = iso()
        digest = card_hash(document.payload)
        served = cardmod.card_summary(document.payload)["name"]
        try:
            previous = cardmod.card_summary(json.loads(row["card"]))["name"]
        except (TypeError, ValueError, KeyError):
            previous = served
        # This method now serves the same-url path too, where a changed digest
        # usually means the card was edited rather than renamed. The audit log is
        # the one place the difference is visible, so record it honestly.
        changed = "renamed" if net.name_key(served) != net.name_key(previous) else "changed"
        with self.db.connect() as connection:
            if digest == row["card_hash"]:
                # Every repeat registration of a renamed agent arrives here (the
                # derived id never matches the stored one), so an unchanged card must
                # not claim a change from X to X — and, like the same-url path in
                # ``register``, a repeat moves no timestamp at all, not even
                # ``checked_at``. One rule for "nothing changed", whichever door the
                # request came through.
                outcome = "existing"
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
                        now,
                        now,
                        now,
                        row["id"],
                    ),
                )
                outcome = changed
            fresh = connection.execute(
                "SELECT * FROM agents WHERE id = ?", (row["id"],)
            ).fetchone()
            if fresh is None:
                # A concurrent delete removed the entry; nothing holds a transaction
                # across the UPDATE and this read.
                raise RegistryError(404, "NOT_FOUND", f"no agent with id {row['id']}")
            self._record_registration(connection, ip, row["host"], now, outcome)
        return Registration(record=self._record(fresh), manage_token=None, created=False)

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

    def _load_by_name(self, host: str, path: str, name: str) -> sqlite3.Row | None:
        """The entry at this ``(host, path)`` whose *stored* card carries this name.

        The id cannot answer this: an entry keeps the id it was born with, so after a
        rename ``derive_id(host, path, name)`` points at nothing. The path is part of
        the match for exactly the reason it is part of the key — an agent served from
        another path on the same host is a *different* agent, and matching it here would
        hand one agent's entry, token and conversations to another.
        """
        wanted = net.name_key(name)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agents WHERE host = ? ORDER BY last_ok_at DESC", (host,)
            ).fetchall()
        for row in rows:
            if net.path_of(row["url"]) != path:
                continue
            try:
                payload = json.loads(row["card"])
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and net.name_key(payload.get("name") or "") == wanted:
                return row
        return None

    def _load_by_url(self, normalized: str) -> sqlite3.Row | None:
        """Find the entry holding this url, whatever id it currently has.

        ``agents.url`` is UNIQUE, so this identifies at most one row. It is how a
        renamed card is recognised: the address is unchanged, the derived id is new,
        and only the row that already owns the address can explain the difference.
        """
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM agents WHERE url = ?", (normalized,)
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

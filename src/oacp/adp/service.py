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


#: The status of an entry that no longer claims its address. Not a ``down``:
#: ``down`` means "unreachable, keep retrying", and the probe loop would flip it
#: back to ``ok`` the first time the *other* agent's card answered.
RETIRED = "retired"


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

        One rule decides every outcome below: **whoever serves the card just fetched
        owns the address.** Identity is the card — ADP reads it from the card, not
        from the address — so a different name at an address is a different agent,
        and the entry that used to be there has to let go of the address while
        keeping everything that is its own (id, ``first_seen_at``, manage token,
        conversations).

        Five outcomes:

        * ``(host, name)`` is new, and nobody live holds the url -> 201, a fresh
          entry with a manage token;
        * it exists under the **same** url -> 200, its card is refreshed and no
          timestamp moves if the bytes did not;
        * it exists under a **different** url -> 200, the entry **moves**: url, card
          and hashes are replaced while the id, ``first_seen_at`` and the manage
          token stay. This is what makes a port change cost nothing;
        * this id exists but is **retired** -> 200, it is **revived**: the identity
          came back and takes the address again (see :meth:`_revive`);
        * a **different** live entry holds the url -> 201, that entry **retires** and
          a new one is created (see :meth:`_replace`). This is the case that used to
          adopt the new card into the old entry, which is exactly how a brand-new
          agent inherited somebody else's conversations.
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
            if existing["status"] == RETIRED:
                # The identity is back. Its id is a pure function of (host, path,
                # name), so it can only ever come back as this row — and whatever
                # took the address meanwhile has to let go.
                return self._revive(existing, normalized, document, ip)
            if existing["url"] == normalized:
                # Same key and same address, but that proves the key, not that the
                # *stored card is current*: a repeat registration can carry an edited
                # card, and the caller holds a document it fetched and validated.
                # ``_adopt_name`` follows it: an unchanged digest records "existing"
                # and moves no timestamp, a changed one replaces the card.
                return self._adopt_name(existing, document, ip)
            return self._move(existing, normalized, document, ip)

        holder = self._load_by_url(normalized)
        if holder is not None:
            # A *different* identity is serving this address now. See ``_replace``.
            return self._replace(holder, agent_id, normalized, document, ip)

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
            # A concurrent request took this id or this url between the lookups above
            # and the INSERT. Those are the only two constraints this statement can
            # violate (``agents_url_live`` is the url one), so re-resolving is exact
            # — and it has to be the *same* resolution the lookups above would have
            # made, or two concurrent callers would answer differently.
            row = self._load(agent_id)
            if row is not None:
                if row["status"] == RETIRED:
                    return self._revive(row, normalized, document, ip)
                if row["url"] != normalized:
                    return self._move(row, normalized, document, ip)
                return self._adopt_name(row, document, ip)
            holder = self._load_by_url(normalized)
            if holder is None:
                raise
            return self._replace(holder, agent_id, normalized, document, ip)

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
            # Two live entries on one host now claim the same name and the same
            # address. Refusing is the only honest answer: silently repointing would
            # drop the row that already owns the url (``agents_url_live`` allows one
            # live holder), and merging would destroy a real entry (§10).
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
                # concurrent registration can take the url in between. Among live rows
                # the url is unique (``agents_url_live``) and that is the only
                # constraint this statement can violate, so the race means precisely
                # what the explicit check above says.
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

    def _replace(
        self,
        row: sqlite3.Row,
        agent_id: str,
        normalized: str,
        document: Any,
        ip: str,
    ) -> Registration:
        """Hand an address to a different agent and retire the entry that had it.

        This used to be an adoption: keep the entry, swap the card. It is how a
        brand-new agent — a different name, a different identity — inherited another
        agent's conversations while the panel showed only the new name. The card is
        the identity (ADP is explicit about that), so the honest answer is a new
        entry.

        Retiring rather than deleting is what lets this be a 201 instead of a 409:
        the previous entry keeps its id, its ``first_seen_at``, its manage token and
        every conversation held with it, and gives up only the address claim
        (``agents_url_live`` is partial). Those conversations stay readable and stay
        unsendable — they point at an agent that is not at that address any more, and
        handing them to the new one is the bug being fixed here, not a courtesy.

        The retire and the insert are **one transaction**. On their own, a failing
        insert — the domain budget, a concurrent registration — would commit the
        retirement and leave the address with no live entry at all: the panel would
        show the old agent as retired and nothing serving the url.
        """
        now = iso()
        token = new_token()
        host = net.host_of(normalized)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Re-read the holder *inside* the write transaction. The row this call
                # was handed was read on another connection, and a concurrent
                # registration in between could have retired it and taken the address
                # with an entry of its own. Retiring by id would then be a no-op and
                # the INSERT below would hit ``agents_url_live`` as a raw
                # IntegrityError — a 500 from a path whose whole job is to resolve
                # exactly this. Under ``BEGIN IMMEDIATE`` no other writer can be in
                # the middle of it, so this read is authoritative.
                holder = connection.execute(
                    "SELECT id FROM agents WHERE url = ? AND status <> ?",
                    (normalized, RETIRED),
                ).fetchone()
                if holder is not None:
                    connection.execute(
                        "UPDATE agents SET status = ? WHERE id = ?",
                        (RETIRED, holder["id"]),
                    )
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
                self._record_registration(connection, ip, host, now, "replaced")
                fresh = connection.execute(
                    "SELECT * FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except sqlite3.IntegrityError:
                # What is left that can collide is the id itself: a concurrent
                # registration of the *same* agent created that entry after this
                # call looked it up. Nothing was written — the transaction rolled
                # back — so answer with what is there now, which is exactly what
                # ``register`` does when it races itself.
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                again = self._load(agent_id)
                if again is None:
                    raise
                if again["status"] == RETIRED:
                    return self._revive(again, normalized, document, ip)
                if again["url"] != normalized:
                    return self._move(again, normalized, document, ip)
                return self._adopt_name(again, document, ip)
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            if fresh is None:
                raise RegistryError(404, "NOT_FOUND", f"no agent with id {agent_id}")
        return Registration(record=self._record(fresh), manage_token=token, created=True)

    def _revive(
        self,
        row: sqlite3.Row,
        normalized: str,
        document: Any,
        ip: str,
    ) -> Registration:
        """Put an identity that came back in charge of an address again.

        A retired entry is not a tombstone: its id is a pure function of
        ``(host, path, name)``, so the same agent can only ever return as the same
        row. ``retired`` therefore means "not serving an address right now", not
        "this identity is gone" — and the reverse of :meth:`_replace` has to exist,
        or an agent that lost an address to another agent could never take it back.

        Two cases, and they are not the same claim:

        * back at its **own** url — the entry that replaced it there lets go, because
          the card served there now is this identity's. This is A -> B -> A;
        * back at **another** url (another port on the same host and path) — that is
          a move, so a live entry already holding that address wins and this gets the
          same 409 ``URL_TAKEN`` a move would.

        Kept: the id, ``first_seen_at`` and the manage token. ``first_seen_at`` is
        when this identity was first seen, not when it last came back; a revival is
        not a birth. ``changed_at`` moves only if the card differs from the one stored
        before retirement, for the reason a repeat registration moves nothing.
        """
        now = iso()
        digest = card_hash(document.payload)
        host = net.host_of(normalized)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                holder = connection.execute(
                    "SELECT id FROM agents WHERE url = ? AND status <> ?",
                    (normalized, RETIRED),
                ).fetchone()
                if holder is not None and holder["id"] != row["id"]:
                    if normalized != row["url"]:
                        raise RegistryError(
                            409,
                            "URL_TAKEN",
                            f"{normalized!r} already belongs to another entry "
                            f"({holder['id']})",
                        )
                    connection.execute(
                        "UPDATE agents SET status = ? WHERE id = ?",
                        (RETIRED, holder["id"]),
                    )
                # A revival turns a retired row into a live one, so the host cap has
                # to be checked here like on every other path that can add a live
                # entry — otherwise churning one address and reviving the losers on
                # other ports grows a host past ``max_agents_per_domain``. It runs
                # *after* the holder is retired: counting the row this call is about
                # to displace would refuse the ordinary A -> B -> A.
                self._assert_domain_budget(connection, host, row["id"])
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
                self._record_registration(connection, ip, host, now, "revived")
                fresh = connection.execute(
                    "SELECT * FROM agents WHERE id = ?", (row["id"],)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            if fresh is None:
                raise RegistryError(404, "NOT_FOUND", f"no agent with id {row['id']}")
        return Registration(record=self._record(fresh), manage_token=None, created=False)

    def _retire(self, row: sqlite3.Row, at: str, *, outcome: str, ip: str) -> None:
        """Stop one entry claiming its address, keeping the row (see ``_replace``)."""
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE agents SET status = ? WHERE id = ?", (RETIRED, row["id"])
            )
            self._record_registration(connection, ip, row["host"], at, outcome)

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
        """The same identity, re-registered: refresh the card in place.

        Reached only when the derived id *and* the url both match, so the name is
        unchanged (up to folding) and the agent is not a different one. What can
        still differ is the rest of the card — a description, a skill, an endpoint —
        and a repeat registration must not answer with a card the caller has since
        fetched over and seen replaced.

        A *different* name never arrives here any more: it derives another id, so the
        url lookup in ``register`` finds this entry and hands the address to the new
        agent through ``_replace``. Identity is the card, not the address — that is
        the whole difference between this release and the last one.
        """
        now = iso()
        digest = card_hash(document.payload)
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
                outcome = "changed"
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
        if row["status"] == RETIRED:
            # A retired entry has no address left to check: the url it still spells
            # belongs to the agent that took it over, and reading that card in would
            # undo the retirement with a fresh timestamp.
            return None
        checked = iso()
        try:
            document = self._fetch_card(row["url"])
            self._assert_card(document.payload)
        except (FetchError, RegistryError):
            self._mark_failure(agent_id, checked)
            return self.get(agent_id)

        served = cardmod.card_summary(document.payload)["name"]
        if (
            net.derive_id(net.host_of(row["url"]), net.path_of(row["url"]), served)
            != agent_id
        ):
            # The address now answers with somebody else's card. Adopting it here
            # would be the bug this release removes, arriving through the probe loop
            # instead of a registration: the name in the panel would change while the
            # id — and every conversation hanging off it — stayed. So this entry steps
            # back, and the agent actually serving the address becomes an entry of its
            # own when it registers. Until then the panel shows this one as retired,
            # which is the truth: nobody holds the address on its behalf.
            self._retire(row, checked, outcome="replaced", ip="probe")
            return None

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

    def get(
        self, agent_id: str, *, include_retired: bool = False
    ) -> dict[str, Any] | None:
        """One full AgentRecord, or None.

        A retired entry is deliberately **not** an agent: everything that means "send
        something to this agent" reads through here, and a row whose url now serves
        somebody else must not come back from it. Returning it would put a message
        addressed to agent A into agent B's task while filing it under A's id — the
        failure this status exists to prevent, and a silent one.

        The operator's own read paths (the panel's list and detail views) pass
        ``include_retired=True``: they show the row's history, and the send paths
        they lead to still go through the default.
        """
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None or (row["status"] == RETIRED and not include_retired):
            return None
        return self._record(row)

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
        include_retired: bool = False,
    ) -> dict[str, Any]:
        """Search agents; results are ordered by ``firstSeenAt`` desc, ``id`` asc.

        Retired entries are left out by default, because the contract publishes two
        statuses and a retired row is not an agent any more — its address belongs to
        somebody else. The panel asks for them explicitly: an operator who has just
        lost an agent to another one at the same address should be able to see that
        this is what happened, and where the conversations went.
        """
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
        elif not include_retired:
            clauses.append("status <> ?")
            params.append(RETIRED)
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

    def _load_by_url(self, normalized: str) -> sqlite3.Row | None:
        """Find the **live** entry holding this url, whatever id it has.

        ``agents_url_live`` makes that unique among live rows. Retired rows are
        excluded on purpose: retiring one is precisely what frees its address for the
        agent that serves it now, so a retired row must never be mistaken for the
        current holder — it would answer 200 for an address it no longer serves.
        """
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM agents WHERE url = ? AND status <> ?", (normalized, RETIRED)
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
        # Live entries only: a retired row has given up its address, and counting it
        # would let one address that has changed hands a few times exhaust the host's
        # budget against agents that are not there any more.
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE host = ? AND id != ? AND status <> ?",
            (host, agent_id, RETIRED),
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

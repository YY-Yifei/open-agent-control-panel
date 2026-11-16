"""Identity: an entry is keyed on ``(host, card name)``, and the port is not part of it.

The motivation is operational. A locally-run agent restarts on another port several
times a day; keyed by URL, every restart was a *new agent* with a new id, and the
conversations pointing at the old one became unreachable. Keyed by host and name, the
same agent coming back is the same entry.

ADP §4.1 leaves the id's algorithm to the implementation and fixes four properties —
URL-safe, stable within an entry, at most one entry per url, opaque to consumers. All
four still hold: one url normalises to exactly one ``(host, name)``, so ``url -> id``
is still a function. What changed is the converse, which the contract never required:
one id may now cover several urls (one agent seen on successive ports), and the
registry resolves that by treating a re-registration under a new url as the same
entry moving.

These tests pin what that buys and what it costs: the port change that no longer
creates an agent, the rename that deliberately does *not* move the id (§4.1 calls an
id a handle, stable within an entry — not a function of the current content), the
round trip a repeat registration now costs, and the migration's refusal to merge rows
that happen to derive the same id (§10).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from conftest import make_card
from oacp import net
from oacp.db import SCHEMA_VERSION, Database, MigrationError

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"
#: The path of CARD_URL — part of the key, because a host can serve several agents.
CARD_PATH = "/.well-known/agent-card.json"
ELSEWHERE = "https://elsewhere.example/card"
#: The same address as CARD_URL, spelled the way §4.1 says must collapse.
SAME_ADDRESS = "HTTPS://Agent.Example.com:443/.well-known/agent-card.json"
#: The same agent restarted on another port: a different url, one host.
OTHER_PORT = "https://agent.example.com:8443/.well-known/agent-card.json"
#: Three successive ports for one agent — what a real local file looks like.
PORTS = [f"https://agent.example.com:{port}/card" for port in (8000, 8001, 8002)]


def register(client, fetcher, *, url: str = CARD_URL, **overrides):
    """Serve a card at ``url`` and register it through the real HTTP surface."""
    fetcher.cards[url] = make_card(**overrides)
    return client.post("/v1/agents", json={"url": url})


def _conversation(client, agent_id: str, *, session: str = "s_1") -> str:
    """Attach a conversation to an agent — the history the key exists to protect.

    Written directly because the point under test is the pointer, not the chat: a
    session id is what the panel resolves through ``state.agentId``.
    """
    path = client.app.state.settings.db_path
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO sessions (id, agent_id, agent_name, agent_url, title, state,"
            " created_at, updated_at) VALUES (?, ?, 'Geo Agent', ?, 'hello', 'idle',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (session, agent_id, CARD_URL),
        )
    return session


def _owners(client) -> list[str]:
    """Every agent id a conversation currently points at."""
    path = client.app.state.settings.db_path
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT agent_id FROM sessions ORDER BY id")]


# ------------------------------------------------------------------ the key


def test_the_id_is_derived_from_the_host_and_the_card_name(client, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    record = client.post("/v1/agents", json={"url": SAME_ADDRESS}).json()
    host = "agent.example.com"
    expected = (
        "a_" + hashlib.sha256(f"{host}\n{CARD_PATH}\ngeo agent".encode("utf-8")).hexdigest()[:16]
    )
    assert record["id"] == expected == net.derive_id(host, CARD_PATH, "Geo Agent")
    assert record["id"].startswith("a_") and len(record["id"]) == 18
    # The stored url is the normalised one, not the spelling that arrived.
    assert record["url"] == net.normalize_url(CARD_URL)


def test_a_name_folds_whitespace_and_case_before_it_is_hashed() -> None:
    """A cosmetic edit must not be a different agent."""
    assert net.derive_id("h", "/p", "Geo  Agent") == net.derive_id("h", "/p", " geo agent ")
    assert net.name_key("  Geo \t Agent\n") == "geo agent"
    # …but the host and the path are not folded away, and the port is never in the key.
    assert net.derive_id("a.example", "/p", "Geo Agent") != net.derive_id("b.example", "/p", "Geo Agent")
    assert net.derive_id("h", "/a", "Geo Agent") != net.derive_id("h", "/b", "Geo Agent")
    assert net.derive_id(*net_and_name(PORTS[0])) == net.derive_id(*net_and_name(PORTS[1]))


def net_and_name(url: str) -> tuple[str, str, str]:
    """``(host, path, name)`` for a url — the inputs the derivation takes."""
    normalized = net.normalize_url(url)
    return net.host_of(normalized), net.path_of(normalized), "Geo Agent"


def test_url_spelling_variants_are_one_entry(client, fetcher) -> None:
    first = register(client, fetcher).json()
    again = register(client, fetcher, url=SAME_ADDRESS)
    assert again.status_code == 200
    assert again.json()["id"] == first["id"]


def test_one_card_on_two_hosts_is_two_entries(client, fetcher) -> None:
    """A different host is a different agent, whatever the card says.

    This is the boundary the rule keeps: within one host the name decides, across
    hosts it does not.
    """
    first = register(client, fetcher).json()
    second = register(client, fetcher, url=ELSEWHERE)
    assert second.status_code == 201
    body = second.json()
    assert body["id"] != first["id"]
    assert body["card"]["name"] == first["card"]["name"]  # same card…
    assert body["url"] == ELSEWHERE                        # …different entry
    listed = client.get("/v1/agents").json()["items"]
    assert {item["id"] for item in listed} == {first["id"], body["id"]}


def test_a_restart_on_another_port_is_the_same_agent(client, fetcher) -> None:
    """The whole point of the change: a new port is not a new agent.

    The entry moves to the new address, keeps its id and its token, and the
    conversation that hung off it is still reachable — which is the failure this
    rule exists to remove.
    """
    first = register(client, fetcher).json()
    _conversation(client, first["id"])

    fetcher.cards[OTHER_PORT] = make_card()
    del fetcher.cards[CARD_URL]   # the port the agent used to be on is gone
    moved = client.post("/v1/agents", json={"url": OTHER_PORT})
    assert moved.status_code == 200
    body = moved.json()
    assert body["id"] == first["id"], "a port change must not mint a new agent"
    assert body["url"] == net.normalize_url(OTHER_PORT), "the address did move"
    assert "manageToken" not in body, "the token belongs to the entry, which did not change"
    assert body["firstSeenAt"] == first["firstSeenAt"], "first sighting is the agent's, not the url's"
    assert _owners(client) == [first["id"]], "the conversation followed the agent"
    assert len(client.get("/v1/agents").json()["items"]) == 1


def test_a_rename_at_a_stable_address_keeps_the_id(client, fetcher) -> None:
    """An id is a handle, stable within an entry (§4.1) — not a function of the card.

    The address belongs to this entry, so the agent did not move; only its name did.
    Re-keying would re-point every conversation for a cosmetic edit, which is the
    fragility this whole change removes. The price is asserted too: the id still
    spells the old name's digest.
    """
    first = register(client, fetcher).json()
    _conversation(client, first["id"])
    fetcher.cards[CARD_URL] = make_card(name="Renamed Agent")

    again = client.post("/v1/agents", json={"url": CARD_URL})
    assert again.status_code == 200
    body = again.json()
    assert body["id"] == first["id"]
    assert body["card"]["name"] == "Renamed Agent"
    assert body["url"] == net.normalize_url(CARD_URL)
    assert body["changedAt"] is not None and body["prevHash"] == first["cardHash"]
    assert _owners(client) == [first["id"]]
    assert body["id"] == net.derive_id("agent.example.com", CARD_PATH, "Geo Agent"), (
        "the id is the handle it was born with, not the current name's digest"
    )


def test_a_repeat_registration_is_still_idempotent(client, fetcher) -> None:
    first = register(client, fetcher).json()
    again = register(client, fetcher).json()
    assert again["id"] == first["id"]
    assert again["checkedAt"] == first["checkedAt"], "a repeat must not move any timestamp"
    assert again["changedAt"] is None
    # The name lives in the card, so the entry cannot be located without fetching it:
    # what used to be answerable from storage now costs one round trip. Asserted, not
    # hidden — and the reason a repeat registration of an offline agent now fails.
    assert fetcher.calls.count(CARD_URL) == 2


def test_a_rename_at_the_same_url_is_followed_by_probing(client, fetcher) -> None:
    first = register(client, fetcher).json()
    fetcher.cards[CARD_URL] = make_card(name="Renamed Agent")

    refreshed = client.post(f"/api/panel/agents/{first['id']}/refresh")
    assert refreshed.status_code == 200
    record = refreshed.json()["agent"]
    assert record["id"] == first["id"]           # the entry did not move
    assert record["card"]["name"] == "Renamed Agent"  # the card did
    assert record["changedAt"] == record["lastOkAt"]
    assert record["prevHash"] == first["cardHash"]


# ---------------------------------------------------------------- summaries


def test_a_summary_has_exactly_the_contracts_eleven_fields(client, fetcher) -> None:
    """AgentSummary is closed (additionalProperties: false) — no extra keys."""
    register(client, fetcher)
    summary = client.get("/v1/agents").json()["items"][0]
    assert set(summary) == {
        "id",
        "url",
        "name",
        "description",
        "skills",
        "tags",
        "status",
        "cardHash",
        "checkedAt",
        "lastOkAt",
        "failures",
    }
    full = client.get(f"/v1/agents/{summary['id']}").json()
    assert {"card", "firstSeenAt", "changedAt", "prevHash"} <= set(full)



# ------------------------------------------------- what the review taught us


def test_a_pure_port_change_does_not_report_a_card_change(client, fetcher) -> None:
    """The card is byte-identical across a restart; "changed from X to X" is a lie.

    Without this, every restart would put the entry into every ``changedSince``
    result even though nothing about the agent changed.
    """
    first = register(client, fetcher).json()
    fetcher.cards[OTHER_PORT] = make_card()
    del fetcher.cards[CARD_URL]

    moved = client.post("/v1/agents", json={"url": OTHER_PORT}).json()

    assert moved["id"] == first["id"]
    assert moved["cardHash"] == first["cardHash"]
    assert moved["changedAt"] is None
    assert moved["prevHash"] is None


def test_a_repeat_after_a_rename_moves_no_timestamp(client, fetcher) -> None:
    """A renamed entry derives a new id forever, so every repeat re-enters that path."""
    first = register(client, fetcher).json()
    fetcher.cards[CARD_URL] = make_card(name="Renamed Agent")

    renamed = client.post("/v1/agents", json={"url": CARD_URL}).json()
    again = client.post("/v1/agents", json={"url": CARD_URL}).json()

    assert again["id"] == first["id"]
    assert again["changedAt"] == renamed["changedAt"], "a repeat must not invent a change"
    assert again["checkedAt"] == renamed["checkedAt"], "a repeat must not move any timestamp"


def test_a_card_renamed_onto_a_neighbours_name_is_refused_not_a_crash(client, fetcher) -> None:
    """Two entries on one host now claim one name and one address.

    ``agents.url`` is UNIQUE, so repointing would drop a real row, and merging would
    destroy one (§10). The answer is a 409 the caller can act on — not a 500.
    """
    register(client, fetcher)                                   # (host, "Geo Agent")
    register(client, fetcher, url=OTHER_PORT, name="Other Agent")
    fetcher.cards[OTHER_PORT] = make_card(name="Geo Agent")     # renamed onto its neighbour

    response = client.post("/v1/agents", json={"url": OTHER_PORT})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "URL_TAKEN"
    urls = {item["url"] for item in client.get("/v1/agents").json()["items"]}
    assert urls == {net.normalize_url(CARD_URL), net.normalize_url(OTHER_PORT)}


def test_names_that_differ_only_by_unicode_normalisation_are_one_agent() -> None:
    """Case-folding does not normalise: NFD and NFC are one name spelled two ways."""
    assert net.derive_id("h", "/p", "Caf\u00e9") == net.derive_id("h", "/p", "Cafe\u0301")


def test_an_entry_still_serving_at_its_old_url_cannot_be_moved(client, fetcher) -> None:
    """A same-name card next door is a second agent, not a restart.

    Requiring the old address to have stopped answering is what keeps the
    port-change rule from doubling as a way to repoint somebody else's entry.
    """
    first = register(client, fetcher).json()
    fetcher.cards[OTHER_PORT] = make_card()      # both addresses alive

    response = client.post("/v1/agents", json={"url": OTHER_PORT})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "AGENT_STILL_AT_OLD_URL"
    detail = client.get(f"/v1/agents/{first['id']}").json()
    assert detail["url"] == net.normalize_url(CARD_URL), "the entry did not move"
    assert len(client.get("/v1/agents").json()["items"]) == 1


def test_a_renamed_agent_that_changes_address_stays_one_entry(client, fetcher) -> None:
    """The id stops being derivable after a rename, so the stored name has to find it.

    Register at U1 as "Geo Agent" -> rename to "Renamed Agent" at U1 -> restart on U2
    still called "Renamed Agent". Without the by-name lookup the agent splits in two,
    with the history stranded on the dead half.
    """
    first = register(client, fetcher).json()
    _conversation(client, first["id"])

    fetcher.cards[CARD_URL] = make_card(name="Renamed Agent")
    renamed = client.post("/v1/agents", json={"url": CARD_URL}).json()
    assert renamed["id"] == first["id"]

    fetcher.cards[OTHER_PORT] = make_card(name="Renamed Agent")
    del fetcher.cards[CARD_URL]
    moved = client.post("/v1/agents", json={"url": OTHER_PORT})

    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["id"] == first["id"]
    assert body["url"] == net.normalize_url(OTHER_PORT)
    assert len(client.get("/v1/agents").json()["items"]) == 1, "one agent, one entry"
    assert _owners(client) == [first["id"]]


def test_two_agents_on_one_host_at_different_paths_stay_two_entries(client, fetcher) -> None:
    """The path is part of the key: a shared hostname is not a shared agent.

    Behind a reverse proxy one host serves many agents and the port distinguishes
    nothing — the path does. Folding paths together would let one agent's
    registration move the other's entry, and its conversations, onto a new agent.
    """
    one = "https://agent.example.com/one/card.json"
    two = "https://agent.example.com/two/card.json"
    first = register(client, fetcher, url=one).json()
    second = register(client, fetcher, url=two)

    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]
    assert len(client.get("/v1/agents").json()["items"]) == 2

# ------------------------------------------------------------- migration


def _older_database(path: Path, agents: list[tuple[str, str, str]], *, version: int = 7) -> None:
    """A file from a release that keyed ids by URL: one conversation per agent."""
    Database(str(path)).init()
    connection = sqlite3.connect(str(path))
    for index, (agent_id, url, name) in enumerate(agents, start=1):
        stamp = f"2026-01-{index:02d}T00:00:00Z"
        connection.execute(
            "INSERT INTO agents (id, url, host, card, card_hash, status, first_seen_at,"
            " checked_at, last_ok_at, failures, manage_token)"
            " VALUES (?, ?, ?, ?, 'sha256:x', 'ok', ?, ?, ?, 0, 'token')",
            (
                agent_id,
                url,
                net.host_of(net.normalize_url(url)),
                json.dumps(make_card(name=name)),
                stamp,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO sessions (id, agent_id, agent_name, agent_url, title, state,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'hello', 'idle', ?, ?)",
            (f"s_{index}", agent_id, name, url, stamp, stamp),
        )
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()


def test_opening_a_url_keyed_database_rekeys_it_by_host_and_name(tmp_path) -> None:
    path = tmp_path / "old.db"
    _older_database(path, [("a_0123456789abcdef", CARD_URL, "Geo Agent")])

    Database(str(path)).init()

    new_id = net.derive_id("agent.example.com", CARD_PATH, "Geo Agent")
    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("SELECT id FROM agents").fetchone()[0] == new_id
        assert connection.execute("SELECT agent_id FROM sessions").fetchone()[0] == new_id
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        outcomes = [row[0] for row in connection.execute("SELECT outcome FROM registrations")]
        assert outcomes == ["rekeyed"]

    # The pass is idempotent: opening the file again moves nothing.
    Database(str(path)).init()
    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
        assert connection.execute("SELECT id FROM agents").fetchone()[0] == new_id
        outcomes = [row[0] for row in connection.execute("SELECT outcome FROM registrations")]
        assert outcomes == ["rekeyed"]


def test_rows_for_one_agent_on_several_ports_keep_one_primary_entry(tmp_path) -> None:
    """A file written across several port changes holds several rows for one agent.

    §10 forbids merging (and merging would throw away a manage token), so every row
    survives: the most recently successful one takes the derived id, the rest take
    ``<id>_2``, ``<id>_3``. Conversations are re-pointed at the row that carried them,
    never at an unrelated entry.
    """
    path = tmp_path / "ports.db"
    _older_database(
        path,
        [
            ("a_0000000000000001", PORTS[0], "Geo Agent"),
            ("a_0000000000000002", PORTS[1], "Geo Agent"),
            ("a_0000000000000003", PORTS[2], "Geo Agent"),
        ],
    )

    Database(str(path)).init()

    base = net.derive_id("agent.example.com", "/card", "Geo Agent")
    with sqlite3.connect(str(path)) as connection:
        ids = {row[0] for row in connection.execute("SELECT id FROM agents")}
        assert ids == {base, f"{base}_2", f"{base}_3"}, "no row may be dropped"
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3
        assert set(row[0] for row in connection.execute("SELECT agent_id FROM sessions")) <= ids
        primary = connection.execute(
            "SELECT url FROM agents WHERE id = ?", (base,)
        ).fetchone()[0]
    assert primary == PORTS[2], "the most recent success is the entry a registration resolves to"


def test_two_hosts_serving_one_card_stay_two_entries(tmp_path) -> None:
    """The name rule does not reach across hosts — that is the boundary it keeps."""
    path = tmp_path / "hosts.db"
    _older_database(
        path,
        [
            ("a_0000000000000001", CARD_URL, "Geo Agent"),
            ("a_0000000000000002", ELSEWHERE, "geo   agent"),
        ],
    )

    Database(str(path)).init()

    expected = {
        net.derive_id("agent.example.com", CARD_PATH, "Geo Agent"),
        net.derive_id("elsewhere.example", "/card", "geo   agent"),
    }
    with sqlite3.connect(str(path)) as connection:
        ids = {row[0] for row in connection.execute("SELECT id FROM agents")}
        assert ids == expected
        survivors = [row[0] for row in connection.execute("SELECT agent_id FROM sessions")]
        assert len(survivors) == 2
        assert set(survivors) <= expected


def test_a_derived_id_collision_aborts_instead_of_merging(tmp_path, monkeypatch) -> None:
    """ADP §10: the merge judgement is the row, never the id — so stop on a collision.

    Two *different* hosts and names deriving one id is the 64-bit caveat in §4.1, not
    the several-ports case (which is one group by construction). Merging would delete a
    real entry, so the pass must refuse and leave the file at its old schema version for
    the next start to finish.
    """
    path = tmp_path / "collision.db"
    _older_database(
        path,
        [
            ("a_0000000000000001", CARD_URL, "Geo Agent"),
            ("a_0000000000000002", ELSEWHERE, "Another Agent"),
        ],
    )

    monkeypatch.setattr(net, "derive_id", lambda host, path, name: "a_0123456789abcdef")

    with pytest.raises(MigrationError):
        Database(str(path)).init()

    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        urls = {row[0] for row in connection.execute("SELECT url FROM agents")}
        assert urls == {CARD_URL, ELSEWHERE}, "a collision must not merge or drop an entry"


def test_a_rename_back_to_the_old_name_follows_the_card(client, fetcher) -> None:
    """A -> B -> A must end holding the "A" card, not the stale "B" one.

    The id is the digest of the name the entry was *born* with, so renaming back
    derives that same id again while the address never moved: id and url both match,
    and reading that as "nothing changed" would answer 200 with the card stored for
    "B" — a document the caller fetched over, validated, and then never saw stored.
    """
    first = register(client, fetcher).json()

    fetcher.cards[CARD_URL] = make_card(name="Renamed Agent")
    renamed = client.post("/v1/agents", json={"url": CARD_URL}).json()
    assert renamed["id"] == first["id"]

    fetcher.cards[CARD_URL] = make_card(name="Geo Agent")
    back = client.post("/v1/agents", json={"url": CARD_URL}).json()

    assert back["id"] == first["id"], "an id is a handle, not a function of the card"
    assert back["card"]["name"] == "Geo Agent", "the fetched card must be what is stored"
    stored = client.get(f"/v1/agents/{first['id']}").json()
    assert stored["card"]["name"] == "Geo Agent"
    assert len(client.get("/v1/agents").json()["items"]) == 1, "one agent, one entry"


def test_an_id_collision_is_refused_rather_than_used_to_repoint(
    client, fetcher, monkeypatch
) -> None:
    """Two different (host, path) deriving one id is a collision, not a move.

    The digest is 64 bits, so this should never happen — but "same id" must not be
    read as "same agent". Repointing would hand the first entry's id, manage token
    and conversations to whoever registered the colliding address, and the victim
    could never take them back. §10 says stop.
    """
    first = register(client, fetcher).json()
    monkeypatch.setattr(net, "derive_id", lambda *args, **kwargs: first["id"])

    response = register(client, fetcher, url=ELSEWHERE)

    assert response.status_code == 500, response.text
    assert response.json()["code"] == "ID_COLLISION"
    detail = client.get(f"/v1/agents/{first['id']}").json()
    assert detail["url"] == net.normalize_url(CARD_URL), "the entry did not move"
    assert len(client.get("/v1/agents").json()["items"]) == 1, "no entry was added"

"""Identity is the URL: ADP §4.1, as published.

The registry id is ``a_`` + the first 16 hex characters of ``sha256(normalised
url)``. Two spellings of one address converge on one entry; two addresses that
serve the same card are two entries. These tests pin that rule — including the
case 0.3.0 got wrong, when it keyed entries on the card's name.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from conftest import make_card
from oacp import net
from oacp.db import SCHEMA_VERSION, Database

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"
ELSEWHERE = "https://elsewhere.example/card"
#: The same address as CARD_URL, spelled the way §4.1 says must collapse.
SAME_ADDRESS = "HTTPS://Agent.Example.com:443/.well-known/agent-card.json"


def register(client, fetcher, *, url: str = CARD_URL, **overrides):
    """Serve a card at ``url`` and register it through the real HTTP surface."""
    fetcher.cards[url] = make_card(**overrides)
    return client.post("/v1/agents", json={"url": url})


# ------------------------------------------------------------------ the key


def test_the_id_is_derived_from_the_normalised_url(client, fetcher) -> None:
    # The card is served at the normalised address; the request spells it differently.
    fetcher.cards[CARD_URL] = make_card()
    record = client.post("/v1/agents", json={"url": SAME_ADDRESS}).json()
    normalized = net.normalize_url(CARD_URL)
    expected = "a_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    assert record["id"] == expected == net.derive_id(normalized)
    assert record["id"].startswith("a_") and len(record["id"]) == 18
    # The stored url is the normalised one, not the spelling that arrived.
    assert record["url"] == normalized


def test_url_spelling_variants_are_one_entry(client, fetcher) -> None:
    first = register(client, fetcher).json()
    again = register(client, fetcher, url=SAME_ADDRESS)
    assert again.status_code == 200
    assert again.json()["id"] == first["id"]


def test_one_card_on_two_addresses_is_two_entries(client, fetcher) -> None:
    """§4.1 is explicit: a new URL is a new entry, whatever the card says."""
    first = register(client, fetcher).json()
    second = register(client, fetcher, url=ELSEWHERE)
    assert second.status_code == 201
    body = second.json()
    assert body["id"] != first["id"]
    assert body["card"]["name"] == first["card"]["name"]  # same card…
    assert body["url"] == ELSEWHERE                        # …different entry
    listed = client.get("/v1/agents").json()["items"]
    assert {item["id"] for item in listed} == {first["id"], body["id"]}


def test_a_repeat_registration_is_answered_from_storage(client, fetcher) -> None:
    first = register(client, fetcher).json()
    again = register(client, fetcher).json()
    assert again["id"] == first["id"]
    assert again["checkedAt"] == first["checkedAt"]
    assert again["changedAt"] is None
    # The id is computable from the URL, so a repeat needs no fetch at all.
    assert fetcher.calls.count(CARD_URL) == 1


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


# ------------------------------------------------------------- migration


def _schema4_database(path: Path, agents: list[tuple[str, str, str]]) -> None:
    """A 0.3.0 file: ids hashed from the card name, one conversation per agent."""
    Database(str(path)).init()
    connection = sqlite3.connect(str(path))
    for index, (agent_id, url, name) in enumerate(agents, start=1):
        stamp = f"2026-01-{index:02d}T00:00:00Z"
        connection.execute(
            "INSERT INTO agents (id, url, host, card, card_hash, status, first_seen_at,"
            " checked_at, last_ok_at, failures, manage_token)"
            " VALUES (?, ?, 'example.com', ?, 'sha256:x', 'ok', ?, ?, ?, 0, 'token')",
            (agent_id, url, json.dumps(make_card(name=name)), stamp, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO sessions (id, agent_id, agent_name, agent_url, title, state,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'hello', 'idle', ?, ?)",
            (f"s_{index}", agent_id, name, url, stamp, stamp),
        )
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()


def test_opening_a_0_3_0_database_rekeys_it_by_url(tmp_path) -> None:
    path = tmp_path / "old.db"
    _schema4_database(path, [("a_0123456789abcdef", CARD_URL, "Geo Agent")])

    Database(str(path)).init()

    new_id = net.derive_id(CARD_URL)
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


def test_two_rows_that_shared_a_name_stay_two_entries(tmp_path) -> None:
    """The name rule merged them; the URL rule keeps them apart."""
    path = tmp_path / "old.db"
    _schema4_database(
        path,
        [
            ("a_0000000000000001", CARD_URL, "Geo Agent"),
            ("a_0000000000000002", ELSEWHERE, "geo   agent"),
        ],
    )

    Database(str(path)).init()

    expected = {net.derive_id(CARD_URL), net.derive_id(ELSEWHERE)}
    with sqlite3.connect(str(path)) as connection:
        ids = {row[0] for row in connection.execute("SELECT id FROM agents")}
        assert ids == expected, "two addresses serving one card are two entries"
        survivors = [row[0] for row in connection.execute("SELECT agent_id FROM sessions")]
        assert len(survivors) == 2
        assert set(survivors) <= expected

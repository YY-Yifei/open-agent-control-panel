"""Identity is the agent's name: the URL is an attribute, not the key.

Until schema 4 the id was ``sha256(url)``, which meant an agent that went offline
and came back on another host came back as a *different* agent — its conversations
left behind as orphans. These tests pin the replacement rule and the two cases it
has to get right: a move is the same agent, a rename is not.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import make_card
from oacp.adp import card as cardmod
from oacp.db import Database

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"
ELSEWHERE = "https://elsewhere.example/card"


def register(client, fetcher, *, url: str = CARD_URL, **overrides):
    """Serve a card at ``url`` and register it through the real HTTP surface."""
    fetcher.cards[url] = make_card(**overrides)
    return client.post("/v1/agents", json={"url": url})


# ------------------------------------------------------------------ the key

def test_the_id_is_derived_from_the_name(client, fetcher) -> None:
    record = register(client, fetcher).json()
    assert record["id"] == cardmod.derive_id("Geo Agent")
    assert record["id"].startswith("a_") and len(record["id"]) == 18


def test_case_and_spacing_do_not_forge_a_second_agent() -> None:
    assert cardmod.derive_id("  Geo   Agent ") == cardmod.derive_id("geo agent")
    assert cardmod.derive_id("Geo Agent") != cardmod.derive_id("Geo Agent II")


def test_an_empty_name_has_no_identity() -> None:
    for broken in ("", "   ", None, 42):
        try:
            cardmod.derive_id(broken)
        except ValueError:
            continue
        raise AssertionError(f"derive_id accepted {broken!r}")


# ------------------------------------------------------------------ a move

def test_a_move_keeps_the_id_and_the_conversation(client, fetcher) -> None:
    first = register(client, fetcher).json()
    session = client.post(f"/api/panel/agents/{first['id']}/sessions", json={}).json()["session"]

    moved = register(client, fetcher, url=ELSEWHERE)
    assert moved.status_code == 200
    body = moved.json()
    assert body["id"] == first["id"]
    assert body["url"] == ELSEWHERE
    # The token belongs to whoever created the entry, so a move never hands one out.
    assert "manageToken" not in body
    assert body["changedAt"] is not None
    assert body["prevHash"] == first["cardHash"]

    # The conversation is still the same agent's conversation.
    listed = client.get("/api/panel/sessions", params={"agentId": first["id"]}).json()["items"]
    assert [item["id"] for item in listed] == [session["id"]]
    assert client.get(f"/v1/agents/{first['id']}").json()["url"] == ELSEWHERE


def test_a_repeat_registration_at_the_same_url_writes_nothing(client, fetcher) -> None:
    first = register(client, fetcher).json()
    again = register(client, fetcher).json()
    assert again["id"] == first["id"]
    assert again["checkedAt"] == first["checkedAt"]
    assert again["changedAt"] is None


# ---------------------------------------------------------------- a rename

def test_a_rename_at_the_same_url_is_reported_not_swallowed(client, fetcher) -> None:
    first = register(client, fetcher).json()

    response = register(client, fetcher, name="Renamed Agent")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "URL_TAKEN"
    assert body["detail"]["existingId"] == first["id"]
    assert body["detail"]["newId"] == cardmod.derive_id("Renamed Agent")

    # The stored entry is untouched: a rename is a new agent, not an overwrite.
    stored = client.get(f"/v1/agents/{first['id']}").json()
    assert stored["name"] == "Geo Agent"
    assert stored["url"] == CARD_URL


# ------------------------------------------------------------- migration

def _legacy_database(path: Path, agents: list[tuple[str, str, str]]) -> None:
    """A schema-3 database: ids hashed from the URL, one conversation per agent."""
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
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()


def test_opening_an_old_database_rekeys_it_by_name(tmp_path) -> None:
    path = tmp_path / "old.db"
    _legacy_database(path, [("a_0123456789abcdef", CARD_URL, "Geo Agent")])

    Database(str(path)).init()

    new_id = cardmod.derive_id("Geo Agent")
    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("SELECT id FROM agents").fetchone()[0] == new_id
        assert connection.execute("SELECT agent_id FROM sessions").fetchone()[0] == new_id
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        outcomes = [row[0] for row in connection.execute("SELECT outcome FROM registrations")]
        assert outcomes == ["rekeyed"]

    # The pass is idempotent: opening the file again moves nothing.
    Database(str(path)).init()
    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
        assert connection.execute("SELECT id FROM agents").fetchone()[0] == new_id


def test_two_old_rows_with_one_name_are_merged(tmp_path) -> None:
    path = tmp_path / "old.db"
    _legacy_database(
        path,
        [
            ("a_0000000000000001", CARD_URL, "Geo Agent"),
            ("a_0000000000000002", ELSEWHERE, "geo   agent"),
        ],
    )

    Database(str(path)).init()

    new_id = cardmod.derive_id("Geo Agent")
    with sqlite3.connect(str(path)) as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM agents")]
        assert ids == [new_id]
        # Both conversations survive, pointing at the entry that survived.
        survivors = [row[0] for row in connection.execute("SELECT agent_id FROM sessions")]
        assert survivors == [new_id, new_id]

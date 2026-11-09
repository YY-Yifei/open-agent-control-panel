"""Removing an agent: with the manage token, or on the panel's operator authority.

The contract's rule (``DELETE /v1/agents/{id}``) is untouched: only the holder of
the manage token may remove an entry. These tests pin down the *panel's* extra
path, which exists because an operator has to be able to clear entries nobody can
prove ownership of — a self-registering agent, or one registered from another
browser — and pin down exactly who is allowed to use it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import FakeFetcher, make_card
from oacp.app import create_app
from oacp.config import Settings

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"


def _register(client, fetcher) -> dict:
    fetcher.cards[CARD_URL] = make_card()
    created = client.post("/v1/agents", json={"url": CARD_URL})
    assert created.status_code == 201
    return created.json()


def _ids(client) -> list[str]:
    return [item["id"] for item in client.get("/api/panel/agents").json()["items"]]


def _operator_settings(tmp_path, **overrides) -> Settings:
    values = {
        "db_path": str(tmp_path / "removal.db"),
        "probe_enabled": False,
        "allow_private_agents": True,
        "register_per_hour": 1000,
        "a2a_timeout": 5.0,
    }
    values.update(overrides)
    return Settings(**values)


def _offline(app, url: str = CARD_URL) -> dict:
    """Serve one card without touching the network; return it."""
    fake = FakeFetcher()
    card = make_card(url=url)
    fake.cards[url] = card
    app.state.registry.fetcher = fake
    return fake.cards


# ------------------------------------------------------------- manage token path


def test_removal_with_the_manage_token(client, fetcher) -> None:
    created = _register(client, fetcher)
    agent_id = created["id"]

    response = client.request(
        "DELETE", f"/api/panel/agents/{agent_id}", json={"token": created["manageToken"]}
    )
    assert response.status_code == 204
    assert _ids(client) == []
    assert client.get(f"/v1/agents/{agent_id}").status_code == 404


def test_a_wrong_manage_token_is_reported_in_chinese(client, fetcher) -> None:
    created = _register(client, fetcher)
    agent_id = created["id"]

    response = client.request(
        "DELETE", f"/api/panel/agents/{agent_id}", json={"token": "not-the-token"}
    )
    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == "manage_token_mismatch"
    # The panel surface answers in the UI's language, even though the registry
    # behind it speaks the contract's English.
    assert any("\u4e00" <= char <= "\u9fff" for char in payload["detail"])
    # A failed attempt removes nothing.
    assert _ids(client) == [agent_id]

    english = client.request(
        "DELETE",
        f"/api/panel/agents/{agent_id}",
        json={"token": "not-the-token"},
        headers={"Accept-Language": "en"},
    ).json()
    assert english["detail"].startswith("the manage token does not match")


def test_unauthorized_removal_does_not_reveal_whether_an_entry_exists(client) -> None:
    """Authority is settled before existence, so ids cannot be probed by 404s."""
    response = client.request("DELETE", "/api/panel/agents/a_doesnotexist1", json={})
    assert response.status_code == 401
    assert response.json()["code"] == "manage_token_required"


# ------------------------------------------------- provisional authority refusal


def test_an_open_network_panel_may_not_remove_without_a_token(client, fetcher) -> None:
    """Public deployment, no panel token: the manage token is the only way in."""
    created = _register(client, fetcher)
    agent_id = created["id"]

    response = client.request("DELETE", f"/api/panel/agents/{agent_id}")
    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == "manage_token_required"
    assert any("\u4e00" <= char <= "\u9fff" for char in payload["detail"])
    assert _ids(client) == [agent_id]

    # Sending an empty body is the same as sending nothing — not a 422.
    assert client.request("DELETE", f"/api/panel/agents/{agent_id}", json={}).status_code == 401
    assert (
        client.request(
            "DELETE", f"/api/panel/agents/{agent_id}", json={"token": ""}
        ).status_code
        == 401
    )


def test_the_summary_says_what_removal_needs(client) -> None:
    summary = client.get("/api/panel/summary").json()
    assert summary["removal"] == {"operator": False, "reason": "manage-token"}


# -------------------------------------------------------------- operator paths


def test_local_mode_lets_the_operator_remove_without_a_token(tmp_path) -> None:
    settings = _operator_settings(tmp_path, allow_insecure_cards=True)
    app = create_app(settings)
    with TestClient(app) as client:
        card_url = "http://127.0.0.1:9000/.well-known/agent-card.json"
        _offline(app, card_url)
        created = client.post("/v1/agents", json={"url": card_url}).json()
        agent_id = created["id"]

        assert client.get("/api/panel/summary").json()["removal"] == {
            "operator": True,
            "reason": "local-mode",
        }
        assert client.request("DELETE", f"/api/panel/agents/{agent_id}").status_code == 204
        assert _ids(client) == []

        # The removal is on the record, in the same log as registrations.
        with app.state.db.connect() as connection:
            outcomes = [
                row["outcome"]
                for row in connection.execute(
                    "SELECT outcome FROM registrations ORDER BY id"
                ).fetchall()
            ]
        assert outcomes == ["created", "removed-by-operator"]


def test_a_panel_token_lets_the_operator_remove_without_a_token(tmp_path) -> None:
    settings = _operator_settings(tmp_path, panel_token="correct-horse-battery-staple")
    app = create_app(settings)
    admin = {"X-Panel-Token": "correct-horse-battery-staple"}
    with TestClient(app) as client:
        _offline(app)
        created = client.post("/v1/agents", json={"url": CARD_URL}).json()
        agent_id = created["id"]

        # Without the credential the panel does not even answer.
        assert client.request("DELETE", f"/api/panel/agents/{agent_id}").status_code == 401
        assert (
            client.request(
                "DELETE", f"/api/panel/agents/{agent_id}", headers={"X-Panel-Token": "nope"}
            ).status_code
            == 401
        )

        response = client.request("DELETE", f"/api/panel/agents/{agent_id}", headers=admin)
        assert response.status_code == 204
        assert client.get(f"/v1/agents/{agent_id}", headers=admin).status_code == 404
        assert client.get("/api/panel/summary", headers=admin).json()["removal"] == {
            "operator": True,
            "reason": "panel-token",
        }


def test_the_protocol_surface_still_demands_the_manage_token(tmp_path) -> None:
    """Local mode grants the *panel* authority; it must not leak into /v1."""
    settings = _operator_settings(tmp_path, allow_insecure_cards=True)
    app = create_app(settings)
    with TestClient(app) as client:
        _offline(app)
        created = client.post("/v1/agents", json={"url": CARD_URL}).json()
        agent_id = created["id"]

        assert client.delete(f"/v1/agents/{agent_id}").status_code == 401
        assert client.delete(
            f"/v1/agents/{agent_id}", headers={"X-ADP-Token": "wrong"}
        ).status_code == 401
        assert client.get(f"/v1/agents/{agent_id}").status_code == 200


def test_removing_a_missing_entry_as_operator_is_404(tmp_path) -> None:
    settings = _operator_settings(tmp_path, allow_insecure_cards=True)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.request("DELETE", "/api/panel/agents/a_doesnotexist1")
        assert response.status_code == 404
        assert response.json()["code"] == "agent_not_found"
        assert "a_doesnotexist1" in response.json()["detail"]


# ------------------------------------------------------------------ the ledger


def test_tasks_outlive_the_agent_they_ran_on(client, fetcher) -> None:
    created = _register(client, fetcher)
    agent_id = created["id"]

    with client.app.state.db.connect() as connection:
        connection.execute(
            """
            INSERT INTO tasks (
                id, agent_id, agent_name, agent_url, state, title, input_text,
                created_at, updated_at, message_count
            ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, 1)
            """,
            (
                "t_kept",
                agent_id,
                "Geo Agent",
                CARD_URL,
                "alpha",
                "alpha",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )

    response = client.request(
        "DELETE", f"/api/panel/agents/{agent_id}", json={"token": created["manageToken"]}
    )
    assert response.status_code == 204

    listing = client.get("/api/panel/tasks").json()["items"]
    assert [item["id"] for item in listing] == ["t_kept"]
    task = client.get("/api/panel/tasks/t_kept").json()["task"]
    # The task row carries its own copy of the agent, so history stays readable.
    assert task["agentName"] == "Geo Agent"
    assert task["agentId"] == agent_id

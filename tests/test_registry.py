"""The ADP Core operations, exercised through the real HTTP surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import FakeFetcher, make_card
from oacp.app import create_app
from oacp.config import Settings

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"


def register(client, url: str = CARD_URL):
    return client.post("/v1/agents", json={"url": url})


def test_registry_card_is_self_describing(client, settings) -> None:
    response = client.get("/.well-known/a2a-registry.json")
    assert response.status_code == 200
    body = response.json()
    assert body["apiBase"] == settings.api_base
    assert body["spec"] == settings.spec_url
    assert body["probeIntervalSeconds"] == settings.probe_interval
    assert body["limits"] == {
        "maxCardBytes": settings.max_card_bytes,
        "maxAgentsPerDomain": settings.max_agents_per_domain,
    }
    assert set(body) <= {"name", "apiBase", "spec", "probeIntervalSeconds", "limits", "contact"}


def test_registration_is_idempotent_and_returns_one_token(client, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()

    first = register(client)
    assert first.status_code == 201
    created = first.json()
    assert created["id"].startswith("a_") and len(created["id"]) == 18
    assert created["cardHash"].startswith("sha256:")
    assert created["manageToken"]
    assert first.headers["location"] == f"/v1/agents/{created['id']}"
    assert created["changedAt"] is None
    assert created["prevHash"] is None
    assert created["status"] == "ok"
    assert created["failures"] == 0
    assert created["firstSeenAt"] == created["lastOkAt"] == created["checkedAt"]
    # AgentRecord is closed: the facets live on the card, not beside it.
    assert "name" not in created and "skills" not in created
    assert created["card"]["name"] == "Geo Agent"
    assert created["card"]["skills"][0]["id"] == "geo.lookup"

    second = register(client)
    assert second.status_code == 200
    assert "manageToken" not in second.json()
    assert second.json()["id"] == created["id"]
    # The id is derived from the URL, so storage answers a repeat without a fetch.
    assert fetcher.calls.count(CARD_URL) == 1
    assert second.json()["changedAt"] is None


def test_registration_requires_https(client) -> None:
    response = register(client, "http://agent.example.com/card")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "BAD_URL"
    assert "https" in body["message"]


def test_registration_reports_unreachable_cards(client) -> None:
    response = register(client)
    assert response.status_code == 422
    assert response.json()["code"] == "CARD_UNREACHABLE"


def test_registration_lists_every_card_problem(client, fetcher) -> None:
    fetcher.cards[CARD_URL] = {"description": "no name, no version"}
    response = register(client)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "NOT_AN_AGENT_CARD"
    problems = body["detail"]["problems"]
    assert any(problem.startswith("name") for problem in problems)
    assert any(problem.startswith("version") for problem in problems)


def test_list_supports_every_documented_filter_and_cursor(client, fetcher) -> None:
    cards = {
        "https://a.example/card": make_card(name="Alpha", url="https://a.example/a2a"),
        "https://b.example/card": make_card(
            name="Beta", url="https://b.example/a2a", description="weather forecasts"
        ),
        "https://c.example/card": make_card(
            name="Gamma",
            url="https://c.example/a2a",
            skills=[{"id": "time.now", "name": "Now", "tags": ["clock"]}],
        ),
    }
    for url, card in cards.items():
        fetcher.cards[url] = card
        assert register(client, url).status_code == 201

    items = client.get("/v1/agents").json()["items"]
    assert len(items) == 3
    assert {
        "id", "url", "name", "description", "skills", "tags", "status",
        "cardHash", "checkedAt", "lastOkAt", "failures",
    } <= set(items[0])
    assert "card" not in items[0]

    assert [item["name"] for item in client.get("/v1/agents", params={"q": "beta"}).json()["items"]] == ["Beta"]
    assert [item["name"] for item in client.get("/v1/agents", params={"skill": "time.now"}).json()["items"]] == ["Gamma"]
    assert [item["name"] for item in client.get("/v1/agents", params={"tag": "clock"}).json()["items"]] == ["Gamma"]

    full = client.get("/v1/agents", params={"q": "alpha", "full": True}).json()["items"][0]
    assert full["card"]["name"] == "Alpha"

    page_one = client.get("/v1/agents", params={"limit": 2}).json()
    assert len(page_one["items"]) == 2 and page_one["nextCursor"]
    page_two = client.get("/v1/agents", params={"limit": 2, "cursor": page_one["nextCursor"]}).json()
    assert len(page_two["items"]) == 1 and page_two["nextCursor"] is None
    seen = {item["id"] for item in page_one["items"]} | {item["id"] for item in page_two["items"]}
    assert len(seen) == 3

    bad_cursor = client.get("/v1/agents", params={"cursor": "not-a-cursor"})
    assert bad_cursor.status_code == 400


def test_get_unknown_agent_returns_404(client) -> None:
    response = client.get("/v1/agents/a_0000000000000000")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_delete_needs_the_manage_token(client, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    created = register(client).json()
    agent_id = created["id"]

    assert client.delete(f"/v1/agents/{agent_id}").status_code == 401
    assert client.delete(f"/v1/agents/{agent_id}", headers={"X-ADP-Token": "wrong"}).status_code == 401
    assert client.delete(
        f"/v1/agents/{agent_id}", headers={"X-ADP-Token": created["manageToken"]}
    ).status_code == 204
    assert client.get(f"/v1/agents/{agent_id}").status_code == 404


def test_probe_follows_content_drift(client, app, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    created = register(client).json()

    fetcher.cards[CARD_URL] = make_card(description="A new description")
    record = app.state.registry.probe(created["id"])

    assert record is not None
    assert record["card"]["description"] == "A new description"
    assert record["changedAt"] is not None
    assert record["prevHash"] == created["cardHash"]
    assert record["cardHash"] != created["cardHash"]
    assert record["lastOkAt"] == record["changedAt"]


def test_three_consecutive_failures_mark_agent_down(client, app, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    agent_id = register(client).json()["id"]

    fetcher.failures[CARD_URL] = "unreachable"
    first = app.state.registry.probe(agent_id)
    second = app.state.registry.probe(agent_id)
    assert first["status"] == "ok" and first["failures"] == 1
    assert second["status"] == "ok" and second["failures"] == 2

    third = app.state.registry.probe(agent_id)
    assert third["status"] == "down" and third["failures"] == 3

    fetcher.failures.pop(CARD_URL)
    recovered = app.state.registry.probe(agent_id)
    assert recovered["status"] == "ok" and recovered["failures"] == 0


def test_changed_since_is_an_instant_comparison(client, app, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    created = register(client).json()

    # Never changed yet: changedAt is null, so no changedSince query returns it.
    assert client.get("/v1/agents", params={"changedSince": "2000-01-01T00:00:00Z"}).json()["items"] == []

    fetcher.cards[CARD_URL] = make_card(description="drifted")
    changed = app.state.registry.probe(created["id"])
    boundary = changed["changedAt"]

    with_boundary = client.get("/v1/agents", params={"changedSince": boundary}).json()["items"]
    assert [item["id"] for item in with_boundary] == [created["id"]]

    later = client.get("/v1/agents", params={"changedSince": "2999-01-01T00:00:00Z"}).json()["items"]
    assert later == []


def test_changed_since_accepts_offsets(client, app, fetcher) -> None:
    fetcher.cards[CARD_URL] = make_card()
    created = register(client).json()
    fetcher.cards[CARD_URL] = make_card(description="drifted")
    app.state.registry.probe(created["id"])

    # +08:00 must be parsed as an instant, not compared as a string.
    response = client.get("/v1/agents", params={"changedSince": "2000-01-01T08:00:00+08:00"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_dev_mode_allows_http_cards(tmp_path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "dev.db"),
        probe_enabled=False,
        allow_insecure_cards=True,
        allow_private_agents=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        fake = FakeFetcher()
        url = "http://127.0.0.1:9000/.well-known/agent-card.json"
        fake.cards[url] = make_card(url="http://127.0.0.1:9000/a2a")
        app.state.registry.fetcher = fake

        response = client.post("/v1/agents", json={"url": url})
        assert response.status_code == 201, response.text
        assert response.json()["url"] == url

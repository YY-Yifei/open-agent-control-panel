"""The backend face of Agent Native: where to connect, and what search may say.

The page owns the socket and the capabilities (see ``tests/js``); the server owns
exactly two things here — resolving the one URL the page should dial, and the
cross-agent search the page cannot do for itself. Both are small, and both have a
failure mode worth pinning down:

* the aaas token must never reach the browser (it is a bearer credential for a
  service that can drive the whole registry);
* a LIKE search must treat ``%`` and ``_`` as the characters the user typed, or a
  lone ``%`` quietly returns the entire ledger.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import make_card
from oacp.app import create_app
from oacp.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def build(tmp_path, **overrides) -> TestClient:
    """A panel whose settings differ from the fixture's only where asked."""
    base = {
        "db_path": str(tmp_path / "oacp.db"),
        "probe_enabled": False,
        "allow_private_agents": True,
        "register_per_hour": 1000,
        "a2a_timeout": 5.0,
    }
    base.update(overrides)
    return TestClient(create_app(Settings(**base)))


# ------------------------------------------------------------ where to connect


def test_an_unconfigured_server_points_at_the_default_local_port(client) -> None:
    body = client.get("/api/panel/agent").json()

    assert body == {
        "enabled": True,
        "wsUrl": "ws://localhost:8888",
        "viaProxy": False,
        "policy": "act",
        "modelHint": "",
    }


def test_a_custom_url_is_passed_through_verbatim(tmp_path) -> None:
    with build(tmp_path, aaas_url="ws://127.0.0.1:9999/agent") as local:
        assert local.get("/api/panel/agent").json()["wsUrl"] == "ws://127.0.0.1:9999/agent"


def test_a_token_makes_the_page_dial_this_panel_instead(tmp_path) -> None:
    """The browser cannot set ``Authorization``, so a token means the local proxy.

    The important half is the second assertion: the endpoint is unauthenticated,
    so anything it returns is readable by anyone who can reach the port.
    """
    with build(tmp_path, aaas_token="s3cret-token") as local:
        response = local.get("/api/panel/agent")

    body = response.json()
    # A same-origin path on purpose: only the browser knows its own origin, and
    # building the absolute URL here would mean trusting `Host` (client-supplied)
    # — with the panel token riding along in that URL.
    assert body["wsUrl"] == "/api/panel/agent/ws"
    assert body["viaProxy"] is True
    assert "s3cret-token" not in response.text
    assert body.get("token") is None


def test_a_switched_off_panel_offers_no_address_at_all(tmp_path) -> None:
    """``--no-agent`` means the feature is absent, so there is nothing to dial."""
    with build(tmp_path, agent_enabled=False) as local:
        body = local.get("/api/panel/agent").json()

    assert body["enabled"] is False
    assert body["wsUrl"] is None


def test_the_policy_travels_with_the_address(tmp_path) -> None:
    with build(tmp_path, agent_policy="read") as local:
        assert local.get("/api/panel/agent").json()["policy"] == "read"


# ------------------------------------------------------------ settings contract


def test_an_unknown_policy_is_refused_rather_than_guessed(tmp_path) -> None:
    """A typo must not silently widen or narrow what the model may touch."""
    with pytest.raises(ValueError, match="agent_policy"):
        Settings(db_path=str(tmp_path / "x.db"), agent_policy="admin")


def test_a_non_socket_url_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="aaas_url"):
        Settings(db_path=str(tmp_path / "x.db"), aaas_url="http://localhost:8888")


def test_policy_and_url_are_normalised(tmp_path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "x.db"),
        agent_policy="  READ ",
        aaas_url="  ws://localhost:7777 ",
    )

    assert settings.agent_policy == "read"
    assert settings.aaas_url == "ws://localhost:7777"


def test_the_command_line_offers_the_agent_flags() -> None:
    """The flags exist where an operator will look for them.

    They hang off ``serve`` rather than the top-level parser, because that is the
    command that starts a panel — ``oacp --help`` alone would never show them.
    """
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "oacp", "serve", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    for flag in ("--aaas-url", "--aaas-token", "--agent-policy", "--no-agent"):
        assert flag in result.stdout, flag


# ------------------------------------------------------------ the socket proxy
#
# Every one of these is a close *before* accept, so the client sees a refusal
# rather than an established socket that then goes quiet.


def close_code(client: TestClient, path: str, **kwargs) -> int:
    """Connect, wait for the refusal, and report the close code."""
    try:
        with client.websocket_connect(path, **kwargs) as socket:
            socket.receive_text()
    except WebSocketDisconnect as exc:
        return exc.code
    raise AssertionError("the socket was accepted; it should have been refused")


def test_the_proxy_is_not_mounted_without_a_token(tmp_path) -> None:
    """Nothing to proxy to: the page dials aaas directly in that case."""
    with build(tmp_path) as local:
        assert close_code(local, "/api/panel/agent/ws", subprotocols=["aaas.v1"]) == 4404


def test_the_proxy_refuses_a_switched_off_panel(tmp_path) -> None:
    with build(tmp_path, agent_enabled=False, aaas_token="t") as local:
        assert close_code(local, "/api/panel/agent/ws", subprotocols=["aaas.v1"]) == 4404


def test_the_proxy_requires_the_agreed_subprotocol(tmp_path) -> None:
    """Without ``aaas.v1`` the server would not know which protocol to speak."""
    with build(tmp_path, aaas_token="t") as local:
        assert close_code(local, "/api/panel/agent/ws") == 4400


def test_the_proxy_refuses_a_page_from_another_origin(tmp_path) -> None:
    """Any web page can reach 127.0.0.1; only this deployment may drive it."""
    with build(tmp_path, aaas_token="t") as local:
        code = close_code(
            local,
            "/api/panel/agent/ws",
            subprotocols=["aaas.v1"],
            headers={"origin": "https://evil.example"},
        )

    assert code == 4403


def test_the_proxy_checks_the_panel_token_in_the_query(tmp_path) -> None:
    """``new WebSocket()`` cannot send headers, so the token rides in the URL."""
    with build(tmp_path, aaas_token="t", panel_token="panel") as local:
        missing = close_code(local, "/api/panel/agent/ws", subprotocols=["aaas.v1"])
        wrong = close_code(
            local, "/api/panel/agent/ws?token=nope", subprotocols=["aaas.v1"]
        )

    assert (missing, wrong) == (4401, 4401)


# ------------------------------------------------------------------- search
#
# Rows are written straight into the ledger: search reads columns, and building a
# whole conversation through the A2A transport would test that instead.


def seed(app, *, sessions=(), tasks=()) -> None:
    with app.state.db.connect() as connection:
        for title in sessions:
            connection.execute(
                "INSERT INTO sessions (id, agent_id, agent_name, agent_url, title, state,"
                " created_at, updated_at, message_count) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"s_{title[:8]}", "a_x", "Geo Agent", "https://x.example/a2a", title,
                 "done", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 2),
            )
        for title in tasks:
            connection.execute(
                "INSERT INTO tasks (id, agent_id, agent_name, agent_url, state, title,"
                " input_text, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"t_{title[:8]}", "a_x", "Geo Agent", "https://x.example/a2a", "done",
                 title, title, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )


def test_search_finds_a_conversation_by_a_word_in_its_title(app, client) -> None:
    seed(app, sessions=["Where is Lisbon?", "Budget review"])

    body = client.get("/api/panel/search", params={"q": "lisbon"}).json()

    assert [item["title"] for item in body["sessions"]] == ["Where is Lisbon?"]
    assert body["counts"]["sessions"] == 1


def test_a_percent_sign_is_a_character_not_a_wildcard(app, client) -> None:
    """Unescaped, ``%`` would match every row and look like a working search."""
    seed(app, sessions=["100% done", "Budget review"])

    body = client.get("/api/panel/search", params={"q": "%"}).json()

    assert [item["title"] for item in body["sessions"]] == ["100% done"]


def test_an_underscore_does_not_match_any_character(app, client) -> None:
    seed(app, sessions=["plan_a", "planb"])

    body = client.get("/api/panel/search", params={"q": "plan_"}).json()

    assert [item["title"] for item in body["sessions"]] == ["plan_a"]


def test_search_does_not_match_the_json_key_names_of_a_card(client, fetcher) -> None:
    """Matching the raw card would return the whole registry for ``name``."""
    fetcher.cards["https://agent.example.com/a2a"] = make_card()
    client.post("/v1/agents", json={"url": "https://agent.example.com/a2a"})

    for word in ("name", "skills", "capabilities", "defaultInputModes"):
        body = client.get("/api/panel/search", params={"q": word}).json()
        assert body["counts"]["agents"] == 0, word


def test_search_still_finds_an_agent_by_the_words_it_shows(client, fetcher) -> None:
    fetcher.cards["https://agent.example.com/a2a"] = make_card()
    client.post("/v1/agents", json={"url": "https://agent.example.com/a2a"})

    body = client.get("/api/panel/search", params={"q": "geography"}).json()

    assert [item["name"] for item in body["agents"]] == ["Geo Agent"]


def test_scope_narrows_what_is_scanned(app, client) -> None:
    seed(app, sessions=["Lisbon trip"], tasks=["Lisbon map"])

    only_tasks = client.get("/api/panel/search", params={"q": "lisbon", "scope": "tasks"}).json()

    assert only_tasks["counts"] == {"agents": 0, "sessions": 0, "tasks": 1}
    assert client.get("/api/panel/search", params={"q": "lisbon"}).json()["counts"] == {
        "agents": 0,
        "sessions": 1,
        "tasks": 1,
    }


def test_an_empty_query_is_refused(app, client) -> None:
    assert client.get("/api/panel/search", params={"q": ""}).status_code == 422


def test_a_whitespace_only_query_does_not_return_the_ledger(app, client) -> None:
    """``q=%20`` passes ``min_length=1``; after the strip it must not become ``%%``."""
    seed(app, sessions=["Lisbon trip"], tasks=["Lisbon map"])

    body = client.get("/api/panel/search", params={"q": "   "}).json()

    assert body["counts"] == {"agents": 0, "sessions": 0, "tasks": 0}
    assert body["agents"] == body["sessions"] == body["tasks"] == []


def test_an_unknown_scope_is_refused(client) -> None:
    response = client.get("/api/panel/search", params={"q": "x", "scope": "everything"})

    assert response.status_code == 422


def test_limit_caps_each_list(app, client) -> None:
    seed(app, sessions=[f"Lisbon {index}" for index in range(5)])

    body = client.get("/api/panel/search", params={"q": "lisbon", "limit": 2}).json()

    assert len(body["sessions"]) == 2
    # The cap is on the answer, not on the truth: the counts still say five.
    assert body["counts"]["sessions"] == 5


def test_a_query_that_matches_nothing_says_so_without_an_error(client) -> None:
    body = client.get("/api/panel/search", params={"q": "zzzz"}).json()

    assert body["counts"] == {"agents": 0, "sessions": 0, "tasks": 0}
    assert body["agents"] == body["sessions"] == body["tasks"] == []


def test_the_search_payload_never_echoes_a_full_card(client, fetcher) -> None:
    """Search results are handles for the page to open, not a second registry."""
    fetcher.cards["https://agent.example.com/a2a"] = make_card()
    client.post("/v1/agents", json={"url": "https://agent.example.com/a2a"})

    body = client.get("/api/panel/search", params={"q": "geo"}).json()

    assert set(body["agents"][0]) == {"id", "name", "url", "status"}
    assert "skills" not in json.dumps(body)

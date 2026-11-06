"""The demo agent registers itself — tested against a real registry, in process.

No network: the panel runs in-process behind ``TestClient`` (itself an
``httpx.Client``), and the registry's card fetcher is the usual ``FakeFetcher``.
What is real is the protocol path: ``POST /v1/agents`` → fetch → validate → store.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oacp.app import create_app  # noqa: E402
from oacp.config import Settings  # noqa: E402

try:  # pytest puts this directory on sys.path, but a direct import should still work
    from conftest import FakeFetcher, make_card  # noqa: E402
except ImportError:  # pragma: no cover - only for `python tests/test_example_agent.py`
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import FakeFetcher, make_card  # type: ignore[no-redef]  # noqa: E402


def load_agent():
    """Import examples/echo_agent.py with a sys.modules entry (dataclasses need it)."""
    name = "echo_agent_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / "echo_agent.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


echo = load_agent()

CARD_URL = "http://127.0.0.1:9100/.well-known/agent-card.json"
REGISTRY = "http://panel"


@pytest.fixture()
def panel(tmp_path):
    """A registry with the demo agent's card already available to its fetcher."""
    settings = Settings(
        db_path=str(tmp_path / "oacp.db"),
        probe_enabled=False,
        allow_private_agents=True,
        allow_insecure_cards=True,
        register_per_hour=1000,
        a2a_timeout=5.0,
    )
    app = create_app(settings)
    fetcher = FakeFetcher()
    card = make_card(name="Echo Agent", url="http://127.0.0.1:9100/a2a", description="echo")
    fetcher.cards[CARD_URL] = card
    app.state.registry.fetcher = fetcher
    return app


@pytest.fixture()
def client(panel):
    """An ``httpx.Client`` that talks to the panel in process, without its lifespan."""
    test_client = TestClient(panel)
    yield test_client
    test_client.close()


def _ids(client: httpx.Client) -> list[str]:
    return [item["id"] for item in client.get(f"{REGISTRY}/v1/agents").json()["items"]]


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


# ------------------------------------------------------------------ pure pieces


def test_card_url_and_reachable_host():
    assert echo.card_url_of("http://127.0.0.1:9100") == CARD_URL
    assert echo.card_url_of("http://127.0.0.1:9100/") == CARD_URL
    assert echo._reachable_host("0.0.0.0") == "127.0.0.1"
    assert echo._reachable_host("::") == "[::1]"
    assert echo._reachable_host("") == "127.0.0.1"
    assert echo._reachable_host("10.0.0.5") == "10.0.0.5"


def test_card_is_served_at_the_well_known_path():
    served = TestClient(echo.app).get("/.well-known/agent-card.json").json()
    assert served["name"] == "Echo Agent"
    assert served["url"].endswith("/a2a")
    assert served["skills"][0]["id"] == "echo.repeat"


# ------------------------------------------------------------- the registration


def test_agent_registers_itself_and_shows_up(client):
    registration = echo.register(client, REGISTRY, CARD_URL)

    assert registration.created is True
    assert registration.status == 201
    assert registration.agent_id
    assert registration.manage_token, "a fresh registration must come with a manage token"
    assert _ids(client) == [registration.agent_id]


def test_second_registration_is_idempotent_and_keeps_the_token_rule(client):
    first = echo.register(client, REGISTRY, CARD_URL)
    second = echo.register(client, REGISTRY, CARD_URL)

    assert second.created is False
    assert second.status == 200
    assert second.agent_id == first.agent_id
    assert second.manage_token is None, "the registry never rotates or re-issues the token"
    assert _ids(client) == [first.agent_id]


def test_liveness_probe_sees_a_deleted_entry(client):
    registration = echo.register(client, REGISTRY, CARD_URL)
    assert echo.still_registered(client, REGISTRY, registration.agent_id) is True

    removed = echo.deregister(client, REGISTRY, registration.agent_id, registration.manage_token)
    assert removed is True
    assert echo.still_registered(client, REGISTRY, registration.agent_id) is False


def test_registration_errors_are_readable(client):
    with pytest.raises(echo.RegistrationError) as caught:
        echo.register(client, REGISTRY, "http://127.0.0.1:9100/not-a-card")
    message = str(caught.value)
    assert "422" in message
    assert "CARD_UNREACHABLE" in message or "NOT_AN_AGENT_CARD" in message


# ------------------------------------------------------------ the background loop


def test_keeper_waits_for_a_panel_that_is_not_up_yet(panel, monkeypatch):
    monkeypatch.setattr(echo, "BACKOFF", (0.01,))
    notes: list[str] = []

    def unreachable(request: httpx.Request):
        raise httpx.ConnectError("the panel is not up", request=request)

    attempts = {"n": 0}

    def client_factory() -> httpx.Client:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Client(transport=httpx.MockTransport(unreachable))
        return TestClient(panel)

    keeper = echo.SelfRegistration(REGISTRY, CARD_URL, heartbeat=0.05, log=notes.append)
    keeper._client = client_factory  # type: ignore[method-assign]
    keeper.start()
    try:
        registration = keeper.wait_until_registered(timeout=5.0)
        assert registration is not None, notes
        assert registration.created is True
        assert any("retrying" in note for note in notes), notes
    finally:
        keeper.stop()


def test_keeper_re_registers_only_after_the_registry_forgets_it(panel, monkeypatch):
    monkeypatch.setattr(echo, "BACKOFF", (0.01,))
    notes: list[str] = []
    keeper = echo.SelfRegistration(REGISTRY, CARD_URL, heartbeat=0.05, log=notes.append)
    keeper._client = lambda: TestClient(panel)  # type: ignore[method-assign]
    keeper.start()
    try:
        first = keeper.wait_until_registered(timeout=5.0)
        assert first is not None and first.created is True

        # A healthy heartbeat writes nothing: the registry still has one entry
        # and no second registration row was needed to keep it there.
        assert _wait_for(lambda: len(_ids_by_client(panel)) == 1)

        with TestClient(panel) as admin:
            assert echo.deregister(admin, REGISTRY, first.agent_id, first.manage_token)
            assert _wait_for(lambda: admin.get(f"{REGISTRY}/v1/agents/{first.agent_id}").status_code == 404)

            assert _wait_for(lambda: first.agent_id in _ids_by_client(panel))
        assert keeper.registration is not None
        assert keeper.registration.created is True, "a re-registration is a fresh 201"
        assert any("re-registered" in note for note in notes), notes
    finally:
        keeper.stop()


def test_keeper_stops_cleanly_and_can_unregister(panel):
    notes: list[str] = []
    keeper = echo.SelfRegistration(
        REGISTRY, CARD_URL, heartbeat=0.05, deregister_on_exit=True, log=notes.append
    )
    keeper._client = lambda: TestClient(panel)  # type: ignore[method-assign]
    keeper.start()
    registration = keeper.wait_until_registered(timeout=5.0)
    assert registration is not None

    keeper.stop()

    with TestClient(panel) as admin:
        assert admin.get(f"{REGISTRY}/v1/agents/{registration.agent_id}").status_code == 404
    assert any("unregistered" in note for note in notes), notes


def _ids_by_client(panel_app) -> list[str]:
    with TestClient(panel_app) as client:
        return _ids(client)

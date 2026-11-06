"""Shared fixtures: a temp database, an app instance and a fake card fetcher."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest
from fastapi.testclient import TestClient

from oacp.adp.fetcher import Document, FetchError
from oacp.app import create_app
from oacp.config import Settings


def make_card(
    name: str = "Geo Agent",
    url: str = "https://agent.example.com/a2a",
    description: str = "Answers geography questions",
    **overrides,
) -> dict:
    """A valid A2A 0.3-style card (the relaxed validator's baseline)."""
    card = {
        "name": name,
        "description": description,
        "version": "1.2.0",
        "url": url,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "geo.lookup",
                "name": "Geo lookup",
                "description": "Look up a place by name",
                "tags": ["geo", "maps"],
            }
        ],
    }
    card.update(overrides)
    return card


class FakeFetcher:
    """Stands in for GuardedFetcher so tests never touch the network."""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.failures: dict[str, str] = {}
        self.calls: list[str] = []

    def fetch(self, url: str, *, expect_json: bool = True) -> Document:
        self.calls.append(url)
        if url in self.failures:
            raise FetchError("CARD_UNREACHABLE", self.failures[url])
        if url not in self.cards:
            raise FetchError("CARD_UNREACHABLE", f"no card served at {url}")
        return Document(url=url, status=200, body=b"{}", payload=self.cards[url], latency_ms=1)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "oacp.db"),
        probe_enabled=False,
        allow_private_agents=True,
        register_per_hour=1000,
        a2a_timeout=5.0,
    )


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fetcher(app) -> FakeFetcher:
    fake = FakeFetcher()
    app.state.registry.fetcher = fake
    return fake

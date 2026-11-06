"""Local-mode behaviour: a loopback bind relaxes the guard, loudly.

The design under test: trust is derived from *where the panel is reachable*,
not granted by a flag the operator might forget. Bound to loopback nobody but
this machine can POST, so the SSRF battery protects nothing there; bound to a
public interface it still applies in full.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from oacp.app import create_app
from oacp.cli import _is_loopback_host, _local_mode_overrides, build_parser
from oacp.config import Settings


def _serve_args(argv: list[str]):
    return build_parser().parse_args(["serve", *argv])


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.0.0.53", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.10", False),
        ("10.0.0.4", False),
        ("panel.example.com", False),
        ("", False),
    ],
)
def test_loopback_host_detection(host: str, expected: bool) -> None:
    assert _is_loopback_host(host) is expected


def test_loopback_bind_relaxes_both_guards(monkeypatch) -> None:
    monkeypatch.delenv("OACP_ALLOW_INSECURE_CARDS", raising=False)
    monkeypatch.delenv("OACP_ALLOW_PRIVATE_AGENTS", raising=False)
    relaxed = _local_mode_overrides(_serve_args(["--host", "127.0.0.1"]))
    assert relaxed == {"allow_insecure_cards": True, "allow_private_agents": True}


def test_public_bind_keeps_the_strict_defaults() -> None:
    assert _local_mode_overrides(_serve_args(["--host", "0.0.0.0"])) == {}


def test_strict_flag_opts_out(monkeypatch) -> None:
    monkeypatch.delenv("OACP_ALLOW_INSECURE_CARDS", raising=False)
    assert _local_mode_overrides(_serve_args(["--host", "127.0.0.1", "--strict"])) == {}


def test_explicit_flags_and_environment_still_win(monkeypatch) -> None:
    monkeypatch.delenv("OACP_ALLOW_INSECURE_CARDS", raising=False)
    monkeypatch.setenv("OACP_ALLOW_PRIVATE_AGENTS", "0")
    # The flag was pinned by the environment: the default must not override it.
    relaxed = _local_mode_overrides(_serve_args(["--host", "127.0.0.1"]))
    assert relaxed == {"allow_insecure_cards": True}

    monkeypatch.setenv("OACP_ALLOW_INSECURE_CARDS", "0")
    assert _local_mode_overrides(_serve_args(["--host", "127.0.0.1"])) == {}


def test_local_mode_is_reported_by_the_summary(tmp_path) -> None:
    strict = Settings(db_path=str(tmp_path / "strict.db"), probe_enabled=False)
    assert strict.local_mode is False

    relaxed = Settings(
        db_path=str(tmp_path / "local.db"),
        probe_enabled=False,
        allow_insecure_cards=True,
    )
    assert relaxed.local_mode is True

    with TestClient(create_app(relaxed)) as client:
        body = client.get("/api/panel/summary").json()
        assert body["localMode"] is True
        assert body["allowPrivateAgents"] is False  # only the flag that was set

    with TestClient(create_app(strict)) as client:
        assert client.get("/api/panel/summary").json()["localMode"] is False


def test_http_card_urls_are_accepted_in_local_mode(tmp_path) -> None:
    """The point of the change: ``http://127.0.0.1`` registers without a flag."""
    settings = Settings(
        db_path=str(tmp_path / "local.db"),
        probe_enabled=False,
        allow_insecure_cards=True,
        register_per_hour=1000,
    )
    app = create_app(settings)
    from conftest import FakeFetcher, make_card

    fetcher = FakeFetcher()
    card_url = "http://127.0.0.1:9100/.well-known/agent-card.json"
    fetcher.cards[card_url] = make_card(url="http://127.0.0.1:9100/a2a")
    app.state.registry.fetcher = fetcher

    with TestClient(app) as client:
        created = client.post("/v1/agents", json={"url": card_url})
        assert created.status_code == 201, created.text
        assert created.json()["url"] == card_url

    # …and the same URL is rejected by a strict deployment.
    strict_app = create_app(Settings(db_path=str(tmp_path / "s.db"), probe_enabled=False))
    with TestClient(strict_app) as client:
        denied = client.post("/v1/agents", json={"url": card_url})
        assert denied.status_code == 400
        assert denied.json()["code"] == "BAD_URL"

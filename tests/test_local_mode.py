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
from oacp.cli import (
    _derived_public_url,
    _is_loopback_host,
    _local_mode_overrides,
    _push_banner,
    _push_override,
    build_parser,
)
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


# ---------------------------------------------------------------- push defaults


def test_push_is_on_unless_switched_off() -> None:
    assert Settings(probe_enabled=False).push_enabled is True
    assert _push_override(_serve_args([])) is None
    assert _push_override(_serve_args(["--push"])) is True
    assert _push_override(_serve_args(["--no-push"])) is False
    # Turning a default off must not be undone by a stale --push in a start script.
    assert _push_override(_serve_args(["--push", "--no-push"])) is False


def test_callback_base_follows_the_bind_address(tmp_path) -> None:
    """The same derivation local mode uses, applied to the callback URL."""
    settings = Settings(db_path=str(tmp_path / "a.db"), probe_enabled=False)

    settings.public_url = _derived_public_url(_serve_args(["--host", "localhost", "--port", "9100"]))
    assert settings.push_callback_base == "http://localhost:9100/a2a/push"

    settings.public_url = _derived_public_url(_serve_args(["--host", "0.0.0.0", "--port", "9100"]))
    assert settings.push_callback_base == "http://127.0.0.1:9100/a2a/push"


def test_push_banner_prints_the_callback_base(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "a.db"), probe_enabled=False)
    assert _push_banner(settings, "127.0.0.1") == ["  callbacks    : http://127.0.0.1:8000/a2a/push"]


def test_push_banner_warns_when_the_base_cannot_be_reached(tmp_path) -> None:
    """A loopback base on a wider bind is a deployment mistake worth printing."""
    settings = Settings(db_path=str(tmp_path / "a.db"), probe_enabled=False)
    lines = _push_banner(settings, "0.0.0.0")
    assert lines[0].endswith("/a2a/push")
    assert any("only agents on this machine" in line for line in lines)


def test_push_banner_respects_a_public_url_and_the_off_switch(tmp_path) -> None:
    public = Settings(
        db_path=str(tmp_path / "a.db"),
        probe_enabled=False,
        public_url="https://panel.example.com",
    )
    assert _push_banner(public, "0.0.0.0") == ["  callbacks    : https://panel.example.com/a2a/push"]

    off = Settings(db_path=str(tmp_path / "b.db"), probe_enabled=False, push_enabled=False)
    assert "off" in _push_banner(off, "127.0.0.1")[0]

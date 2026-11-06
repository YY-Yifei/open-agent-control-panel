"""App-level behaviour: static UI, health, panel token and static assets."""

from __future__ import annotations

from fastapi.testclient import TestClient

from oacp.app import create_app
from oacp.config import Settings


def test_healthz_and_ui_are_served(client) -> None:
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    index = client.get("/")
    assert index.status_code == 200
    assert "Open Agent Control Panel" in index.text

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "API" in script.text

    assert client.get("/favicon.svg").status_code == 200


def test_openapi_is_available(client) -> None:
    schema = client.get("/api/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/v1/agents" in paths
    assert "/.well-known/a2a-registry.json" in paths
    assert "/api/panel/summary" in paths


def test_panel_token_protects_only_the_panel_api(tmp_path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "token.db"),
        probe_enabled=False,
        panel_token="correct-horse-battery-staple",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/panel/summary").status_code == 401
        assert client.get(
            "/api/panel/summary", headers={"X-Panel-Token": "wrong"}
        ).status_code == 401
        assert client.get(
            "/api/panel/summary", headers={"X-Panel-Token": "correct-horse-battery-staple"}
        ).status_code == 200
        assert client.get(
            "/api/panel/summary",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        ).status_code == 200

        # The protocol surface stays open: agents must be able to register.
        assert client.get("/.well-known/a2a-registry.json").status_code == 200
        assert client.get("/v1/agents").status_code == 200


def test_panel_summary_reports_deployment_facts(client) -> None:
    summary = client.get("/api/panel/summary").json()
    assert summary["version"]
    assert summary["apiBase"].endswith("/v1")
    assert summary["registryCard"]["apiBase"] == summary["apiBase"]
    assert summary["cardValidation"] in {"strict", "relaxed"}
    assert summary["panelTokenRequired"] is False

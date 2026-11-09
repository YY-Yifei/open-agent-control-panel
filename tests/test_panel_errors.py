"""Panel error text: Chinese by default, English on request, code always kept.

The rule this file pins down:

* ``/api/panel`` — the panel's own surface — localizes ``detail`` and always
  includes a stable ``code``. Chinese is the default because the UI is.
* ``/v1`` — the ADP contract — keeps its English ``code``/``message`` pair. The
  UI translates those codes for display; the wire does not bend to the UI.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from conftest import make_card
from oacp.errors import LANGS, TEXTS, PanelError, normalize_lang

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def register(client, fetcher, *, url: str = CARD_URL, card: dict | None = None) -> str:
    fetcher.cards[url] = card or make_card()
    response = client.post("/v1/agents", json={"url": url})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ------------------------------------------------------------------- the catalog


def test_catalog_is_fully_bilingual():
    """Every code carries both languages with the same placeholders."""
    assert TEXTS, "the catalog must not be empty"
    for code, entry in TEXTS.items():
        assert set(entry) == set(LANGS), code
        assert set(PLACEHOLDER.findall(entry["zh"])) == set(PLACEHOLDER.findall(entry["en"])), code


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, "zh"),
        ("", "zh"),
        ("zh-CN", "zh"),
        ("zh-CN,zh;q=0.9,en;q=0.8", "zh"),
        ("en", "en"),
        ("en-US,en;q=0.9", "en"),
        ("en;q=0.4,zh;q=0.9", "zh"),
        ("en;q=0", "zh"),
        ("fr-FR", "zh"),  # unrecognised falls back to the panel's default
        ("*", "zh"),
    ],
)
def test_accept_language_is_understood(header, expected):
    assert normalize_lang(header) == expected


def test_panel_error_renders_both_languages_and_keeps_english_for_logs():
    error = PanelError("task_not_refreshable", status=409)
    assert error.status == 409
    assert error.payload("zh") == {
        "detail": "该任务没有远端任务 id，无法刷新",
        "code": "task_not_refreshable",
    }
    assert error.payload("en") == {
        "detail": "this task has no remote task id to refresh",
        "code": "task_not_refreshable",
    }
    # ``str()`` stays English: it lands in logs and tracebacks, not in the UI.
    assert str(error) == "this task has no remote task id to refresh"


# ------------------------------------------------------------ /api/panel speaks zh


def test_unknown_agent_is_chinese_by_default(client):
    response = client.get("/api/panel/agents/nope")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "找不到 id 为 nope 的智能体",
        "code": "agent_not_found",
    }


def test_unknown_agent_speaks_english_when_asked(client):
    response = client.get("/api/panel/agents/nope", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert response.status_code == 404
    assert response.json() == {
        "detail": "no agent with id nope",
        "code": "agent_not_found",
    }


def test_unknown_task_is_localized(client):
    response = client.get("/api/panel/tasks/nope")
    assert response.status_code == 404
    assert response.json()["detail"] == "找不到 id 为 nope 的任务"
    assert response.json()["code"] == "task_not_found"


def test_bad_cursor_is_localized(client, fetcher):
    register(client, fetcher)
    response = client.get("/api/panel/tasks", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    assert response.json() == {"detail": "分页游标无效", "code": "bad_cursor"}


def test_panel_token_error_is_localized_and_keeps_its_header(settings):
    from fastapi.testclient import TestClient

    from oacp.app import create_app

    locked = replace(settings, panel_token="s3cret")
    with TestClient(create_app(locked)) as client:
        bare = client.get("/api/panel/summary")
        assert bare.status_code == 401
        assert bare.headers["www-authenticate"] == "Bearer"
        assert bare.json() == {
            "detail": "需要面板口令（X-Panel-Token 请求头）",
            "code": "panel_token_required",
        }

        english = client.get("/api/panel/summary", headers={"Accept-Language": "en"})
        assert english.json()["detail"] == "a panel token is required"


def test_no_raw_exception_text_leaks_into_the_chinese_panel(client):
    """The old code passed ``str(exc)`` through; the response must be a catalog line."""
    detail = client.get("/api/panel/agents/ghost").json()["detail"]
    assert "no agent with id" not in detail
    assert "ghost" in detail  # the id itself is still useful


# ------------------------------------------------- the ADP contract stays English


def test_adp_surface_keeps_its_english_contract(client, fetcher):
    fetcher.failures[CARD_URL] = "no card served here"
    response = client.post(
        "/v1/agents",
        json={"url": CARD_URL},
        headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "CARD_UNREACHABLE"
    assert body["message"] == "no card served here"  # protocol text, not localized

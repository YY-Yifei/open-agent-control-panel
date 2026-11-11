"""Receiving pictures: what an agent sends back becomes part of the conversation.

The send direction has its own file (``test_attachments.py``). These tests are the
other half: a file part in an artifact, in either dialect, has to end up as a row
a person can look at — and a file the panel will not show has to end up as a row
that says why, not as silence.

Everything here runs against a mocked transport, so "the agent" is the handler
below and no test touches the network.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from conftest import make_card
from oacp.adp import card as cardmod
from oacp.adp.fetcher import Document, FetchError

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"

#: A real PNG header followed by filler: only the magic bytes matter, because the
#: panel sniffs the type and never decodes the picture.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 40
JPEG_B64 = base64.b64encode(JPEG_BYTES).decode("ascii")
PDF_BYTES = b"%PDF-1.7 not a picture"

PUSH_METHODS = {"tasks/pushNotificationConfig/set", "CreateTaskPushNotificationConfig"}
SEND_METHODS = {"message/send", "SendMessage"}
GET_METHODS = {"tasks/get", "GetTask"}


def file_part(data: str = PNG_B64, *, mime: str = "image/png", name: str = "chart.png") -> dict:
    """A v0.3 file part, the older spelling."""
    return {"kind": "file", "file": {"bytes": data, "mimeType": mime, "name": name}}


def raw_part(data: str = PNG_B64, *, mime: str = "image/png", name: str = "chart.png") -> dict:
    """A v1 file part, the newer spelling."""
    return {"raw": data, "mediaType": mime, "filename": name}


def url_part(url: str = "https://cdn.example.com/cat.png", *, mime: str = "image/png") -> dict:
    return {"url": url, "mediaType": mime}


class Agent:
    """A stand-in agent whose answer the test decides.

    ``parts`` is what every answer carries. ``send_state`` is what a ``message/send``
    comes back with and ``get_state`` what a ``GetTask`` does, so a test can watch
    a task finish between two snapshots.
    """

    def __init__(
        self,
        parts: list[dict],
        *,
        send_state: str = "completed",
        get_state: str | None = None,
        push_capable: bool = False,
    ) -> None:
        self.parts = parts
        self.send_state = send_state
        self.get_state = get_state or send_state
        self.push_capable = push_capable
        self.bodies: list[dict] = []

    def install(self, app) -> "Agent":
        app.state.a2a.transport = httpx.MockTransport(self._handle)
        return self

    def _answer(self, body: dict, state: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "kind": "task",
                    "id": "task-1",
                    "contextId": "ctx-1",
                    "status": {"state": state},
                    "artifacts": [{"parts": self.parts}] if self.parts else [],
                },
            },
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.bodies.append(body)
        method = body["method"]
        if method in SEND_METHODS:
            return self._answer(body, self.send_state)
        if method in GET_METHODS:
            return self._answer(body, self.get_state)
        if method in PUSH_METHODS:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}}
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}},
        )


class FakeMediaFetcher:
    """Stands in for the guarded byte fetcher so tests never open a socket."""

    def __init__(self, body: bytes = PNG_BYTES, *, fail: bool = False) -> None:
        self.body = body
        self.fail = fail
        self.calls: list[str] = []

    def fetch(self, url: str, *, expect_json: bool = True) -> Document:
        self.calls.append(url)
        assert expect_json is False, "an image fetch must not ask for JSON"
        if self.fail:
            raise FetchError("CARD_UNREACHABLE", "nope")
        return Document(url=url, status=200, body=self.body, payload={}, latency_ms=1)


# ----------------------------------------------------------------------- helpers


def register(client, fetcher, **overrides) -> dict:
    fetcher.cards[CARD_URL] = make_card(**overrides)
    response = client.post("/v1/agents", json={"url": CARD_URL})
    assert response.status_code in (200, 201), response.text
    return response.json()


def chat(client, agent_id: str, text: str = "what is this?") -> dict:
    response = client.post(f"/api/panel/agents/{agent_id}/sessions", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()["session"]


def last_agent(session: dict) -> dict:
    messages = [m for m in session["messages"] if m["role"] == "agent"]
    assert messages, f"no agent message in {session['messages']}"
    return messages[-1]


# ------------------------------------------------------------------ the two dialects


@pytest.mark.parametrize("spelling", [file_part, raw_part])
def test_a_file_part_becomes_a_picture(client, app, fetcher, spelling) -> None:
    agent = register(client, fetcher)
    Agent([{"kind": "text", "text": "here"}, spelling()]).install(app)

    session = chat(client, agent["id"])
    message = last_agent(session)

    assert message["text"] == "here"
    [file] = message["attachments"]
    assert file["mime"] == "image/png"
    assert file["name"] == "chart.png"
    assert file["size"] == len(PNG_BYTES)
    assert file["skipped"] is None

    served = client.get(f"/api/panel/attachments/{file['id']}")
    assert served.status_code == 200
    assert served.content == PNG_BYTES
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"


def test_an_answer_that_is_only_a_picture_is_not_lost(client, app, fetcher) -> None:
    # The whole point of the receive side: no text at all, and the turn still shows.
    agent = register(client, fetcher)
    Agent([file_part()]).install(app)

    session = chat(client, agent["id"])
    message = last_agent(session)

    assert message["text"] == ""
    assert [f["name"] for f in message["attachments"]] == ["chart.png"]


def test_a_repeated_artifact_is_not_attached_twice(client, app, fetcher) -> None:
    # A2A artifacts accumulate: the second snapshot of a task still carries the
    # first picture. It must not be re-attached to every later message.
    agent = register(client, fetcher)
    fake = Agent([file_part()], send_state="working", get_state="completed").install(app)

    session = chat(client, agent["id"])
    assert len(last_agent(session)["attachments"]) == 1

    synced = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    assert synced["state"] == "done"
    # No third message: the second snapshot said nothing new. The picture stays on
    # the message it arrived with, and is not re-attached anywhere.
    assert len(synced["messages"]) == 2
    assert sum(len(m["attachments"]) for m in synced["messages"]) == 1


def test_two_pictures_under_one_sentence_are_both_kept(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    Agent(
        [
            {"kind": "text", "text": "two views"},
            file_part(),
            file_part(JPEG_B64, mime="image/jpeg", name="second.jpg"),
        ]
    ).install(app)

    message = last_agent(chat(client, agent["id"]))
    assert [f["mime"] for f in message["attachments"]] == ["image/png", "image/jpeg"]


# ------------------------------------------------------- what the panel will not show


def test_a_file_that_is_not_a_picture_leaves_a_chip_not_silence(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    payload = base64.b64encode(PDF_BYTES).decode("ascii")
    Agent([file_part(payload, mime="image/png", name="report.pdf")]).install(app)

    message = last_agent(chat(client, agent["id"]))

    [file] = message["attachments"]
    assert file["skipped"] == "unsupported"
    assert file["name"] == "report.pdf"
    # A row without bytes must not answer with an empty image.
    assert client.get(f"/api/panel/attachments/{file['id']}").status_code == 404


def test_unreadable_base64_is_a_chip_too(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    Agent([file_part("not base64 at all!!")]).install(app)

    [file] = last_agent(chat(client, agent["id"]))["attachments"]
    assert file["skipped"] == "bad_data"


def test_an_oversized_picture_is_named_as_such(client, app, fetcher, monkeypatch) -> None:
    agent = register(client, fetcher)
    Agent([file_part()]).install(app)
    monkeypatch.setattr(app.state.settings, "max_inbound_image_bytes", 16)

    [file] = last_agent(chat(client, agent["id"]))["attachments"]
    assert file["skipped"] == "too_large"


def test_the_per_message_limit_turns_the_rest_into_chips(client, app, fetcher, monkeypatch) -> None:
    agent = register(client, fetcher)
    Agent(
        [
            file_part(),
            file_part(JPEG_B64, mime="image/jpeg", name="second.jpg"),
            file_part(JPEG_B64, mime="image/jpeg", name="third.jpg"),
        ]
    ).install(app)
    monkeypatch.setattr(app.state.settings, "max_inbound_images", 1)

    files = last_agent(chat(client, agent["id"]))["attachments"]
    assert [f["skipped"] for f in files] == [None, "too_many", "too_many"]


# ------------------------------------------------------------------- url-shaped parts


def test_a_url_part_is_fetched_through_the_guard(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    Agent([url_part()]).install(app)
    media_fetcher = FakeMediaFetcher(PNG_BYTES)
    app.state.sessions.fetcher = media_fetcher

    [file] = last_agent(chat(client, agent["id"]))["attachments"]

    assert media_fetcher.calls == ["https://cdn.example.com/cat.png"]
    assert file["skipped"] is None
    assert file["mime"] == "image/png"
    assert file["name"] == "cat.png"  # no name in the part: the URL path named it


def test_a_url_that_cannot_be_fetched_becomes_a_chip(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    Agent([url_part()]).install(app)
    app.state.sessions.fetcher = FakeMediaFetcher(fail=True)

    [file] = last_agent(chat(client, agent["id"]))["attachments"]
    assert file["skipped"] == "unreachable"


def test_a_url_part_is_downloaded_once_per_turn(client, app, fetcher) -> None:
    # A URL part has no identity until it has been downloaded, so a dedupe that
    # only compares bytes would fetch the same picture again on every poll — at
    # the poll rate, from the agent's own server.
    agent = register(client, fetcher)
    Agent([url_part()], send_state="working", get_state="working").install(app)
    media_fetcher = FakeMediaFetcher()
    app.state.sessions.fetcher = media_fetcher

    session = chat(client, agent["id"])
    for _ in range(3):
        client.post(f"/api/panel/sessions/{session['id']}/sync")

    assert media_fetcher.calls == ["https://cdn.example.com/cat.png"]


def test_a_failed_url_fetch_is_retried_on_the_next_snapshot(client, app, fetcher) -> None:
    # Only a file with bytes counts as fetched: a chip saying "could not be
    # fetched" is not an answer, so the next snapshot tries that URL again.
    agent = register(client, fetcher)
    Agent([url_part()], send_state="working", get_state="working").install(app)
    media_fetcher = FakeMediaFetcher(fail=True)
    app.state.sessions.fetcher = media_fetcher

    session = chat(client, agent["id"])
    assert [f["skipped"] for f in last_agent(session)["attachments"]] == ["unreachable"]

    media_fetcher.fail = False
    synced = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    files = [f for m in synced["messages"] for f in m["attachments"] if not f["skipped"]]
    assert [f["name"] for f in files] == ["cat.png"]
    assert len(media_fetcher.calls) == 2


def test_without_a_fetcher_a_url_part_is_still_honest(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    Agent([url_part()]).install(app)
    app.state.sessions.fetcher = None

    [file] = last_agent(chat(client, agent["id"]))["attachments"]
    assert file["skipped"] == "unreachable"


def test_a_bare_url_stays_text(client, app, fetcher) -> None:
    # No media type, no filename: agents also send a url part to mean "a link".
    # Nothing to fetch on, so the conservative reading keeps the old behaviour.
    agent = register(client, fetcher)
    Agent([{"url": "https://example.com/some/page"}]).install(app)

    message = last_agent(chat(client, agent["id"]))
    assert message["attachments"] == []
    assert "example.com/some/page" in message["text"]


# ------------------------------------------------------------------------ the parser


def test_part_file_reads_both_spellings_and_declines_ambiguous_ones() -> None:
    assert cardmod.part_file(file_part()) == {
        "data": PNG_B64,
        "uri": None,
        "mime": "image/png",
        "name": "chart.png",
    }
    assert cardmod.part_file(raw_part()) == {
        "data": PNG_B64,
        "uri": None,
        "mime": "image/png",
        "name": "chart.png",
    }
    assert cardmod.part_file({"kind": "file", "file": {"uri": "https://x/y.png"}}) == {
        "data": None,
        "uri": "https://x/y.png",
        "mime": "",
        "name": "",
    }
    # A text part is not a file; a bare url is not a file either.
    assert cardmod.part_file({"kind": "text", "text": "hi"}) is None
    assert cardmod.part_file({"url": "https://example.com/page"}) is None


def test_result_files_reads_artifacts_and_the_status_message_only() -> None:
    result = {
        "status": {"message": {"parts": [file_part(name="status.png")]}},
        "artifacts": [{"parts": [file_part(name="artifact.png")]}],
        # history repeats earlier messages: reading it would re-deliver every file
        # on every poll.
        "history": [{"parts": [file_part(name="old.png")]}],
    }
    assert [f["name"] for f in cardmod.result_files(result)] == ["artifact.png", "status.png"]


def test_a_file_part_is_never_rendered_as_text() -> None:
    assert cardmod.part_text(file_part()) is None
    assert cardmod.parts_text([{"kind": "text", "text": "hi"}, file_part()]) == "hi"


# --------------------------------------------------------------------------- push


# ----------------------------------------------------------------- the deployment switch


def test_a_deployment_can_refuse_to_fetch_anything(tmp_path) -> None:
    # A panel that must not act as a proxy for remote content still has to show the
    # replies it gets — it just will not GET a URL an agent names. The switch has to
    # reach the service, not only the setting: a fetcher left wired up would make
    # the setting a lie.
    from oacp.app import create_app
    from oacp.config import Settings

    settings = Settings(
        db_path=str(tmp_path / "no-fetch.db"),
        probe_enabled=False,
        fetch_inbound_files=False,
    )
    app = create_app(settings)

    assert settings.fetch_inbound_files is False
    assert app.state.media_fetcher is None
    assert app.state.sessions.fetcher is None


def _token_for_session(app, session_id: str) -> str:
    with app.state.db.connect() as connection:
        row = connection.execute(
            "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row is not None and row["task_id"], "the conversation owns no task"
        token = connection.execute(
            "SELECT push_token FROM tasks WHERE id = ?", (row["task_id"],)
        ).fetchone()
    assert token is not None and token["push_token"], "no callback token was minted"
    return token["push_token"]


def test_a_push_that_only_carries_a_picture_is_applied(client, app, fetcher) -> None:
    agent = register(
        client, fetcher, capabilities={"streaming": False, "pushNotifications": True}
    )
    Agent([], send_state="working", push_capable=True).install(app)
    session = chat(client, agent["id"])
    token = _token_for_session(app, session["id"])

    callback = client.post(
        f"/a2a/push/{token}",
        json={
            "kind": "task",
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {"state": "completed"},
            "artifacts": [{"parts": [file_part()]}],
        },
    )
    assert callback.status_code == 200
    # An image and no words: a text-only duplicate check would call this a retry
    # of the previous snapshot and drop the picture.
    assert callback.json()["status"] == "applied"

    message = last_agent(client.get(f"/api/panel/sessions/{session['id']}").json()["session"])
    assert [f["name"] for f in message["attachments"]] == ["chart.png"]


def test_a_retried_push_does_not_duplicate_the_picture(client, app, fetcher) -> None:
    agent = register(
        client, fetcher, capabilities={"streaming": False, "pushNotifications": True}
    )
    Agent([], send_state="working", push_capable=True).install(app)
    session = chat(client, agent["id"])
    token = _token_for_session(app, session["id"])
    payload = {
        "kind": "task",
        "id": "task-1",
        "contextId": "ctx-1",
        "status": {"state": "completed"},
        "artifacts": [{"parts": [file_part()]}],
    }

    assert client.post(f"/a2a/push/{token}", json=payload).json()["status"] == "applied"
    assert client.post(f"/a2a/push/{token}", json=payload).json()["status"] == "duplicate"

    message = last_agent(client.get(f"/api/panel/sessions/{session['id']}").json()["session"])
    assert len(message["attachments"]) == 1


def test_the_image_quota_holds_across_snapshots(client, app, fetcher) -> None:
    # The quota belongs to the *turn*, not to one snapshot. It used to be a counter
    # that started at zero every poll, so the next snapshot re-read the same
    # artifacts, found "no files stored yet", and accepted pictures the limit had
    # already refused — overshooting the limit and appending a second copy of the
    # answer with the pictures attached.
    limit = int(app.state.settings.max_inbound_images)
    distinct = [
        file_part(
            base64.b64encode(PNG_BYTES + bytes([index])).decode("ascii"),
            name=f"p{index}.png",
        )
        for index in range(limit + 1)
    ]
    agent = register(client, fetcher)
    fake = Agent(distinct, send_state="working", get_state="completed").install(app)

    session = chat(client, agent["id"])
    shown = sum(
        1 for message in session["messages"] for file in message["attachments"] if file["skipped"] is None
    )
    assert shown == limit, "the first snapshot must stop at the limit"

    # A further picture arrives while the task is still running. The turn has
    # already spent its quota, so this one may only ever be a chip.
    fake.parts = distinct + [file_part(PNG_B64, name="late.png")]
    synced = client.post(f"/api/panel/sessions/{session['id']}/sync").json()["session"]
    shown_after = sum(
        1 for message in synced["messages"] for file in message["attachments"] if file["skipped"] is None
    )
    assert shown_after == limit, "a later snapshot must not spend the quota again"
    assert any(
        file["name"] == "late.png" and file["skipped"] == "too_many"
        for message in synced["messages"]
        for file in message["attachments"]
    )


def test_two_unnamed_files_the_panel_refuses_are_two_chips() -> None:
    # The chip's identity used to be built from (reason, mime, name, source) only,
    # so two different unnamed files collapsed into one row — which hid how many
    # files an agent was refused. The bytes are in hand at this point; they decide.
    from oacp import media

    oversized_a = media.intake(b"\x00" * 64, max_bytes=16)
    oversized_b = media.intake(b"\x01" * 64, max_bytes=16)
    assert oversized_a.reason == media.SKIP_TOO_LARGE
    assert oversized_a.sha256 != oversized_b.sha256

    pdf_a = media.intake(b"%PDF-1.7 one", max_bytes=1024)
    pdf_b = media.intake(b"%PDF-1.7 two", max_bytes=1024)
    assert pdf_a.reason == media.SKIP_UNSUPPORTED
    assert pdf_a.sha256 != pdf_b.sha256


def test_the_same_refused_bytes_are_still_one_chip() -> None:
    # Dedupe has to keep working: the same unusable part repeated by every
    # snapshot of a task must not stack up one chip per poll.
    from oacp import media

    first = media.intake(b"\x00" * 64, max_bytes=16)
    again = media.intake(b"\x00" * 64, max_bytes=16)
    assert first.sha256 == again.sha256


def test_malformed_parts_get_identities_of_their_own() -> None:
    # Two refused files that share a type and a name still have to be two chips, or
    # the panel silently reports one refusal where the agent sent two.
    from oacp import media

    first = media.intake_b64("not base64 at all!!", max_bytes=1024)
    second = media.intake_b64("different garbage??", max_bytes=1024)
    assert first.reason == media.SKIP_BAD_DATA
    assert first.sha256 != second.sha256
    assert media.intake_b64("not base64 at all!!", max_bytes=1024).sha256 == first.sha256


def test_an_oversized_part_is_identified_without_decoding_it() -> None:
    from oacp import media

    big_a = "A" * 4000
    big_b = "B" * 4000
    assert media.intake_b64(big_a, max_bytes=16).sha256 != media.intake_b64(big_b, max_bytes=16).sha256

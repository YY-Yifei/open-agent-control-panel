"""Sending pictures: what reaches the agent, what stays on the panel, what is refused.

The receive direction is out of scope here — nothing in these tests puts an image
*into* a conversation from an agent's side, because the panel does not do that yet.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from conftest import make_card
from oacp import media
from oacp.errors import PanelError

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"

#: A real PNG header followed by filler. Only the magic bytes matter: the panel
#: sniffs the type and never decodes the picture itself.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 40


def png(name: str = "cat.png", *, data: str = PNG_B64, mime: str = "image/png") -> dict:
    return {"mime": mime, "name": name, "data": data}


def image_card(**overrides) -> dict:
    """A card that declares image input, which is what the gate looks for."""
    return make_card(**{"defaultInputModes": ["text/plain", "image/png"], **overrides})


class RecordingAgent:
    """A stand-in agent that keeps every request it was sent."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def install(self, app) -> "RecordingAgent":
        app.state.a2a.transport = httpx.MockTransport(self._handle)
        return self

    def message(self) -> dict:
        for body in reversed(self.bodies):
            if body["method"] in {"message/send", "SendMessage"}:
                return body["params"]["message"]
        raise AssertionError("no message/send was recorded")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.bodies.append(body)
        if body["method"] in {"message/send", "SendMessage"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "kind": "task",
                        "id": "task-1",
                        "contextId": "ctx-1",
                        "status": {"state": "completed"},
                        "artifacts": [{"parts": [{"kind": "text", "text": "got it"}]}],
                    },
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}},
        )


def register(client, fetcher, *, card: dict | None = None, url: str = CARD_URL) -> dict:
    fetcher.cards[url] = card or image_card(url=url)
    response = client.post("/v1/agents", json={"url": url})
    assert response.status_code in (200, 201), response.text
    return response.json()


def send(client, agent_id: str, body: dict) -> httpx.Response:
    return client.post(f"/api/panel/agents/{agent_id}/sessions", json=body)


# ------------------------------------------------------------------ the checks


def test_only_four_image_types_are_recognised() -> None:
    assert media.sniff(PNG_BYTES) == "image/png"
    assert media.sniff(JPEG_BYTES) == "image/jpeg"
    assert media.sniff(b"GIF89a" + b"\x00" * 20) == "image/gif"
    assert media.sniff(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == "image/webp"
    assert media.sniff(b"%PDF-1.7") is None


def test_the_bytes_decide_the_type_not_the_label() -> None:
    with pytest.raises(PanelError) as caught:
        media.prepare({"mime": "image/jpeg", "name": "x.jpg", "data": PNG_B64}, max_bytes=1024)
    assert caught.value.code == "attachment_type_mismatch"


def test_a_non_image_is_refused_even_when_labelled_as_one() -> None:
    payload = base64.b64encode(b"%PDF-1.7 not a picture").decode("ascii")
    with pytest.raises(PanelError) as caught:
        media.prepare({"mime": "image/png", "name": "x.png", "data": payload}, max_bytes=1024)
    assert caught.value.code == "attachment_unsupported"


def test_a_file_name_is_a_label_never_a_location() -> None:
    prepared = media.prepare(
        {"mime": "image/png", "name": "../../etc/passwd", "data": PNG_B64}, max_bytes=1024
    )
    assert prepared.name == "passwd.png"


def test_an_oversized_image_is_refused_before_it_is_decoded() -> None:
    # 3/4 of the encoded length is the first, cheap test: no 8 MB string is ever
    # base64-decoded just to discover it was too large.
    huge = "A" * (4 * 1024 * 1024)
    with pytest.raises(PanelError) as caught:
        media.prepare({"mime": "image/png", "name": "big.png", "data": huge}, max_bytes=1024)
    assert caught.value.code == "attachment_too_large"


# ----------------------------------------------------------- what reaches the wire


def test_a_sent_image_arrives_as_a_file_part(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    fake = RecordingAgent().install(app)

    session = send(client, agent["id"], {"text": "look", "attachments": [png()]}).json()["session"]

    message = fake.message()
    assert message["role"] == "user"
    assert message["parts"][0] == {"kind": "text", "text": "look"}
    assert message["parts"][1] == {
        "kind": "file",
        "file": {"bytes": PNG_B64, "mimeType": "image/png", "name": "cat.png"},
    }
    # The panel keeps its own copy, and the conversation says how many.
    assert [(m["role"], m["text"]) for m in session["messages"]] == [
        ("user", "look"),
        ("agent", "got it"),
    ]
    assert len(session["messages"][0]["attachments"]) == 1
    assert session["messages"][0]["attachments"][0]["name"] == "cat.png"


def test_the_newer_dialect_spells_the_same_part_differently(client, app, fetcher) -> None:
    card = image_card(url=CARD_URL, protocolVersion="1.0")
    agent = register(client, fetcher, card=card)
    fake = RecordingAgent().install(app)

    send(client, agent["id"], {"attachments": [png()]})

    parts = fake.message()["parts"]
    # No text, so no text part: an image-only message is not a blank line plus a file.
    assert parts == [{"raw": PNG_B64, "mediaType": "image/png", "filename": "cat.png"}]


def test_an_image_only_message_names_the_conversation(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    RecordingAgent().install(app)

    session = send(client, agent["id"], {"attachments": [png("holiday.png")]}).json()["session"]

    assert session["title"] == "holiday.png"


def test_the_picture_can_be_read_back_from_the_panel(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    RecordingAgent().install(app)
    session = send(client, agent["id"], {"attachments": [png()]}).json()["session"]

    attachment_id = session["messages"][0]["attachments"][0]["id"]
    response = client.get(f"/api/panel/attachments/{attachment_id}")

    assert response.status_code == 200
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_an_unknown_attachment_is_a_404(client) -> None:
    assert client.get("/api/panel/attachments/999").status_code == 404


def test_deleting_the_conversation_deletes_its_pictures(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    RecordingAgent().install(app)
    session = send(client, agent["id"], {"attachments": [png()]}).json()["session"]
    attachment_id = session["messages"][0]["attachments"][0]["id"]

    assert client.delete(f"/api/panel/sessions/{session['id']}").status_code == 204
    assert client.get(f"/api/panel/attachments/{attachment_id}").status_code == 404


# ----------------------------------------------------------------- what is refused


def test_a_card_that_declares_text_only_is_not_sent_an_image(client, app, fetcher) -> None:
    agent = register(client, fetcher, card=make_card(url=CARD_URL))
    fake = RecordingAgent().install(app)

    response = send(client, agent["id"], {"attachments": [png()]})

    assert response.status_code == 415
    assert response.json()["code"] == "agent_no_image_input"
    # Nothing was sent, so the agent never sees a half-addressed message.
    assert fake.bodies == []


def test_a_card_that_declares_nothing_is_still_tried(client, app, fetcher) -> None:
    card = make_card(url=CARD_URL)
    card.pop("defaultInputModes")
    agent = register(client, fetcher, card=card)
    fake = RecordingAgent().install(app)

    response = send(client, agent["id"], {"attachments": [png()]})

    assert response.status_code == 201, response.text
    assert fake.message()["parts"][-1]["kind"] == "file"


def test_too_many_pictures_in_one_message(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    RecordingAgent().install(app)

    response = send(client, agent["id"], {"attachments": [png(f"{i}.png") for i in range(5)]})

    assert response.status_code == 400
    assert response.json()["code"] == "too_many_attachments"


def test_an_empty_message_is_still_empty(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    RecordingAgent().install(app)
    session = send(client, agent["id"], {"text": "first"}).json()["session"]

    # A blank body is checked on the "say something" path, which is where a user
    # can actually hit it; opening a conversation with no words is a different,
    # legitimate request (an empty conversation the user is about to fill).
    response = client.post(
        f"/api/panel/sessions/{session['id']}/messages", json={"text": "   "}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "empty_message"


def test_a_broken_attachment_leaves_no_message_behind(client, app, fetcher) -> None:
    agent = register(client, fetcher)
    RecordingAgent().install(app)
    session = send(client, agent["id"], {"text": "first"}).json()["session"]

    response = send(
        client, agent["id"], {"text": "second", "attachments": [{"data": "not base64!!"}]}
    )

    assert response.status_code == 400
    after = client.get(f"/api/panel/sessions/{session['id']}").json()["session"]
    assert [m["text"] for m in after["messages"]] == ["first", "got it"]

"""Validate live responses against the published ADP contract.

The published ``spec/adp-core.yaml`` is the machine-readable source of truth, so
this test does not ask "does it look right" — it asks whether the published JSON
Schema accepts every body we hand out. Both objects are closed
(``additionalProperties: false``), which is exactly where a hand-rolled response
drifts first: one extra key and a strict consumer rejects the whole record.

The spec is not vendored here (it is another project's source of truth, and a
copy would rot), so the test skips unless it can be found: set ``OACP_ADP_SPEC``
to the YAML, or check the a2a-discover repository out beside this one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import FakeFetcher, make_card

jsonschema = pytest.importorskip("jsonschema")
yaml = pytest.importorskip("yaml")

CARD_URL = "https://agent.example.com/.well-known/agent-card.json"
ROOT = Path(__file__).resolve().parents[1]


def find_spec() -> Path | None:
    candidates = [
        os.environ.get("OACP_ADP_SPEC"),
        ROOT.parents[1] / "A2A Auto Discover" / "spec" / "adp-core.yaml",
        ROOT.parent / "A2A Auto Discover" / "spec" / "adp-core.yaml",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


SPEC_PATH = find_spec()
pytestmark = pytest.mark.skipif(SPEC_PATH is None, reason="published adp-core.yaml not found")

_spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")) if SPEC_PATH else {}


def validator(name: str):
    return jsonschema.Draft202012Validator({"$ref": f"#/components/schemas/{name}", **_spec})


def assert_conforms(instance, name: str) -> None:
    errors = sorted(validator(name).iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, "\n".join(
        f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors
    )


@pytest.fixture()
def live(client):
    """The panel's app with a fake fetcher, so no test touches the network."""
    fetcher = FakeFetcher()
    fetcher.cards[CARD_URL] = make_card()
    client.app.state.registry.fetcher = fetcher
    return client


def test_registry_card_conforms(live) -> None:
    response = live.get("/.well-known/a2a-registry.json")
    assert response.status_code == 200
    assert_conforms(response.json(), "RegistryCard")


def test_registration_bodies_conform(live) -> None:
    created = live.post("/v1/agents", json={"url": CARD_URL})
    assert created.status_code == 201
    assert_conforms(created.json(), "AgentRecordWithToken")
    assert created.headers["location"] == f"/v1/agents/{created.json()['id']}"

    again = live.post("/v1/agents", json={"url": CARD_URL})
    assert again.status_code == 200
    assert_conforms(again.json(), "AgentRecord")
    assert "manageToken" not in again.json()


def test_list_and_record_conform(live) -> None:
    agent_id = live.post("/v1/agents", json={"url": CARD_URL}).json()["id"]

    listed = live.get("/v1/agents")
    assert_conforms(listed.json(), "AgentList")
    summary = listed.json()["items"][0]
    assert "card" not in summary, "a summary must not carry the card"

    full = live.get(f"/v1/agents/{agent_id}")
    assert_conforms(full.json(), "AgentRecord")
    assert "name" not in full.json(), "a record must not carry summary facets"


def test_errors_conform(live) -> None:
    agent_id = live.post("/v1/agents", json={"url": CARD_URL}).json()["id"]

    missing = live.get("/v1/agents/a_0000000000000000")
    assert missing.status_code == 404
    assert_conforms(missing.json(), "Error")

    unauthorized = live.delete(f"/v1/agents/{agent_id}")
    assert unauthorized.status_code == 401
    assert_conforms(unauthorized.json(), "Error")

    bad_url = live.post("/v1/agents", json={"url": "http://agent.example.com/card"})
    assert bad_url.status_code == 400
    assert_conforms(bad_url.json(), "Error")


def test_timestamps_are_second_precision_utc(live) -> None:
    record = live.post("/v1/agents", json={"url": CARD_URL}).json()
    for key in ("firstSeenAt", "checkedAt", "lastOkAt"):
        stamp = record[key]
        assert len(stamp) == 20 and stamp.endswith("Z"), f"{key} is not §4.2-shaped: {stamp}"


def test_an_id_is_opaque_and_need_not_be_derived(live) -> None:
    """§4.1 lets an implementation choose any URL-safe id, so shape is not ours to police.

    None of these entries exist here; the answer must be "no such entry", not
    "malformed id" — a consumer that rejected them would break every alternative
    implementation the relaxed §4.1 now allows.
    """
    for other in ("0193b1a0-7c2e-7d3b-9f00-2c1e4a5b6c7d", "01ARZ3NDEKTSV4RRFFQ69G5FAV", "1"):
        response = live.get(f"/v1/agents/{other}")
        assert response.status_code == 404, f"{other}: {response.status_code}"
        assert_conforms(response.json(), "Error")

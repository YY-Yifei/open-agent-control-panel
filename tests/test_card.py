"""AgentCard validation, interface discovery and A2A result interpretation."""

from __future__ import annotations

from oacp.adp import card as cardmod
from oacp.adp.jcs import card_hash, canonical_json


def base_card() -> dict:
    return {
        "name": "Geo Agent",
        "description": "Answers geography questions",
        "version": "1.2.0",
        "url": "https://agent.example.com/a2a",
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "geo", "name": "Geo", "tags": ["maps"]}],
    }


def test_relaxed_accepts_legacy_card_with_url() -> None:
    assert cardmod.validate_card(base_card(), mode=cardmod.RELAXED) == []


def test_strict_requires_supported_interfaces() -> None:
    problems = cardmod.validate_card(base_card(), mode=cardmod.STRICT)
    assert any(problem.startswith("supportedInterfaces") for problem in problems)


def test_strict_accepts_supported_interfaces_card() -> None:
    card = base_card()
    card.pop("url")
    card["supportedInterfaces"] = [
        {"url": "https://agent.example.com/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0.0"}
    ]
    assert cardmod.validate_card(card, mode=cardmod.STRICT) == []


def test_reports_every_problem_not_just_the_first() -> None:
    card = base_card()
    card.pop("name")
    card.pop("version")
    card["skills"] = [{"id": "", "name": ""}]
    problems = cardmod.validate_card(card)
    assert any(item.startswith("name") for item in problems)
    assert any(item.startswith("version") for item in problems)
    assert any("skills[0].id" in item for item in problems)
    assert any("skills[0].name" in item for item in problems)


def test_duplicate_skill_ids_are_rejected() -> None:
    card = base_card()
    card["skills"] = [{"id": "geo", "name": "Geo"}, {"id": "geo", "name": "Geo again"}]
    problems = cardmod.validate_card(card)
    assert any("duplicate" in item for item in problems)


def test_signatures_must_be_a_list_when_present() -> None:
    card = base_card()
    card["signatures"] = {"not": "a list"}
    assert any(problem.startswith("signatures") for problem in cardmod.validate_card(card))


def test_interfaces_flattens_both_card_generations() -> None:
    legacy = base_card()
    assert cardmod.interfaces(legacy) == [
        {
            "url": "https://agent.example.com/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "",
            "tenant": "",
        }
    ]

    modern = base_card()
    modern.pop("url")
    modern["supportedInterfaces"] = [
        {"url": "https://agent.example.com/grpc", "protocolBinding": "GRPC"},
        {"url": "https://agent.example.com/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0.0"},
    ]
    chosen = cardmod.pick_endpoint(modern)
    assert chosen is not None
    assert chosen["url"] == "https://agent.example.com/a2a"
    assert chosen["style"] == "v1"


def test_pick_endpoint_prefers_jsonrpc_and_detects_style() -> None:
    card = base_card()
    chosen = cardmod.pick_endpoint(card)
    assert chosen is not None and chosen["style"] == "v03"

    card["protocolVersion"] = "1.0"
    chosen = cardmod.pick_endpoint(card)
    assert chosen is not None and chosen["style"] == "v1"


def test_pick_endpoint_returns_none_without_endpoint() -> None:
    assert cardmod.pick_endpoint({"name": "x"}) is None


def test_card_summary_collects_skills_and_tags() -> None:
    card = base_card()
    card["skills"].append({"id": "time", "name": "Time", "tags": ["maps", "clock"]})
    summary = cardmod.card_summary(card)
    assert summary["skills"] == ["geo", "time"]
    assert summary["tags"] == ["maps", "clock"]


def test_card_hash_is_canonical_and_semantic() -> None:
    card = base_card()
    reordered = {key: card[key] for key in reversed(list(card))}
    assert card_hash(card) == card_hash(reordered)
    assert card_hash(card).startswith("sha256:")
    assert canonical_json({"b": 1, "a": [True, None]}) == '{"a":[true,null],"b":1}'
    assert card_hash(card) != card_hash({**card, "version": "9.9.9"})


def test_normalize_state_handles_all_spellings() -> None:
    assert cardmod.normalize_state("TASK_STATE_INPUT_REQUIRED") == "input-required"
    assert cardmod.normalize_state("INPUT_REQUIRED") == "input-required"
    assert cardmod.normalize_state("working") == "working"
    assert cardmod.normalize_state("TASK_STATE_UNSPECIFIED") == "unknown"
    assert cardmod.normalize_state(None) == "unknown"
    assert cardmod.is_terminal("completed")
    assert not cardmod.is_terminal("input-required")


def test_interpret_result_reads_tasks_and_messages() -> None:
    task = {
        "id": "task-1",
        "contextId": "ctx-1",
        "status": {"state": "TASK_STATE_COMPLETED", "message": {"parts": [{"text": "all done"}]}},
        "artifacts": [{"parts": [{"kind": "text", "text": "result text"}]}],
    }
    view = cardmod.interpret_result(task)
    assert view["kind"] == "task"
    assert view["state"] == "completed"
    assert view["text"] == "result text"
    assert view["statusText"] == "all done"

    message = {"role": "agent", "parts": [{"kind": "text", "text": "direct reply"}]}
    view = cardmod.interpret_result(message)
    assert view["kind"] == "message"
    assert view["text"] == "direct reply"

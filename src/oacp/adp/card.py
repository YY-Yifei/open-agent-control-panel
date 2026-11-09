"""AgentCard validation, interface discovery and result interpretation.

ADP Core §6.3 requires a specific set of fields. Real deployments are split
between two generations of A2A cards: the 0.3 shape (``url`` +
``protocolVersion``) and the newer shape (``supportedInterfaces``). The panel
therefore ships two modes:

* ``strict``  — exactly the ADP list, used when you want a strictly conformant
  registry;
* ``relaxed`` — the default: the same checks, but an endpoint may come from
  either shape, and cards that omit optional fields are accepted as long as they
  carry everything needed to actually call the agent.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

STRICT = "strict"
RELAXED = "relaxed"

TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
ACTIVE_STATES = frozenset({"submitted", "working", "input-required", "auth-required"})


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_card(card: Any, *, mode: str = RELAXED) -> list[str]:
    """Return every validation failure as ``path: reason`` (empty = valid).

    Listing *all* failures rather than the first one is deliberate: a client
    fixing its card should not have to play whack-a-mole with the registry.
    """
    if not isinstance(card, dict):
        return ["card: must be a JSON object"]

    strict = mode == STRICT
    problems: list[str] = []

    if not _nonempty_str(card.get("name")):
        problems.append("name: must be a non-empty string")
    if "description" in card and not isinstance(card.get("description"), str):
        problems.append("description: must be a string")
    elif strict and "description" not in card:
        problems.append("description: is required")
    if not _nonempty_str(card.get("version")):
        problems.append("version: must be a non-empty string")

    if strict:
        problems.extend(_validate_supported_interfaces(card))
    else:
        problems.extend(_validate_any_interface(card))

    if "capabilities" in card and not isinstance(card.get("capabilities"), dict):
        problems.append("capabilities: must be an object")
    elif strict and "capabilities" not in card:
        problems.append("capabilities: is required")

    for field in ("defaultInputModes", "defaultOutputModes"):
        value = card.get(field)
        if value is None:
            if strict:
                problems.append(f"{field}: is required")
        elif not _str_list(value) or (strict and not value):
            problems.append(f"{field}: must be a list of strings")

    problems.extend(_validate_skills(card, strict=strict))

    if "signatures" in card and not isinstance(card.get("signatures"), list):
        problems.append("signatures: must be a list")

    return problems


def _validate_supported_interfaces(card: dict[str, Any]) -> list[str]:
    interfaces = card.get("supportedInterfaces")
    if not isinstance(interfaces, list) or not interfaces:
        return ["supportedInterfaces: must be a non-empty list"]
    problems: list[str] = []
    for index, item in enumerate(interfaces):
        path = f"supportedInterfaces[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{path}: must be an object")
            continue
        if not _nonempty_str(item.get("url")):
            problems.append(f"{path}.url: must be a non-empty string")
        if not _nonempty_str(item.get("protocolBinding")):
            problems.append(f"{path}.protocolBinding: must be a non-empty string")
        if "protocolVersion" in item and not isinstance(item.get("protocolVersion"), str):
            problems.append(f"{path}.protocolVersion: must be a string")
    return problems


def _validate_any_interface(card: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    supported = card.get("supportedInterfaces")
    if supported is not None:
        if not isinstance(supported, list):
            problems.append("supportedInterfaces: must be a list")
        else:
            problems.extend(_validate_supported_interfaces({"supportedInterfaces": supported}))
    additional = card.get("additionalInterfaces")
    if additional is not None:
        if not isinstance(additional, list):
            problems.append("additionalInterfaces: must be a list")
        else:
            for index, item in enumerate(additional):
                if not isinstance(item, dict) or not _nonempty_str(item.get("url")):
                    problems.append(f"additionalInterfaces[{index}].url: must be a non-empty string")
    if not interfaces(card):
        problems.append("url: an A2A endpoint is required (supportedInterfaces[].url or url)")
    return problems


def _validate_skills(card: dict[str, Any], *, strict: bool) -> list[str]:
    skills = card.get("skills")
    if skills is None:
        return ["skills: is required"] if strict else []
    if not isinstance(skills, list):
        return ["skills: must be a list"]
    problems: list[str] = []
    seen: set[str] = set()
    for index, skill in enumerate(skills):
        path = f"skills[{index}]"
        if not isinstance(skill, dict):
            problems.append(f"{path}: must be an object")
            continue
        identifier = skill.get("id")
        if not _nonempty_str(identifier):
            problems.append(f"{path}.id: must be a non-empty string")
        elif identifier in seen:
            problems.append(f"{path}.id: duplicate skill id {identifier!r}")
        else:
            seen.add(identifier)
        if not _nonempty_str(skill.get("name")):
            problems.append(f"{path}.name: must be a non-empty string")
        if "tags" in skill and not _str_list(skill.get("tags")):
            problems.append(f"{path}.tags: must be a list of strings")
    return problems


# --------------------------------------------------------------------- discovery


def interfaces(card: dict[str, Any]) -> list[dict[str, str]]:
    """All usable A2A endpoints of a card, in declaration order.

    Both card generations are flattened into one shape so the rest of the code
    never has to branch on the wire version again.
    """
    result: list[dict[str, str]] = []

    supported = card.get("supportedInterfaces")
    if isinstance(supported, list):
        for item in supported:
            if isinstance(item, dict) and _nonempty_str(item.get("url")):
                result.append(
                    {
                        "url": item["url"].strip(),
                        "protocolBinding": str(item.get("protocolBinding") or "JSONRPC"),
                        "protocolVersion": str(item.get("protocolVersion") or ""),
                        "tenant": str(item.get("tenant") or ""),
                    }
                )

    additional = card.get("additionalInterfaces")
    if isinstance(additional, list):
        for item in additional:
            if isinstance(item, dict) and _nonempty_str(item.get("url")):
                result.append(
                    {
                        "url": item["url"].strip(),
                        "protocolBinding": str(item.get("protocolBinding") or ""),
                        "protocolVersion": str(item.get("protocolVersion") or ""),
                        "tenant": str(item.get("tenant") or ""),
                    }
                )

    url = card.get("url")
    if _nonempty_str(url):
        result.append(
            {
                "url": url.strip(),
                "protocolBinding": str(card.get("preferredTransport") or "JSONRPC"),
                "protocolVersion": str(card.get("protocolVersion") or ""),
                "tenant": "",
            }
        )
    return result


def _is_jsonrpc(binding: str) -> bool:
    normalized = binding.replace("+", "-").replace("_", "-").lower()
    return normalized in {"jsonrpc", "json-rpc", "json"}


def pick_endpoint(card: dict[str, Any]) -> dict[str, str] | None:
    """Choose the endpoint to call: a JSON-RPC interface first, then any.

    The returned dict also carries the wire dialect (``style``) so the client can
    build the right ``message/send`` payload without guessing a second time.
    """
    candidates = interfaces(card)
    if not candidates:
        return None
    chosen = next((item for item in candidates if _is_jsonrpc(item["protocolBinding"])), candidates[0])
    return dict(chosen, style=_wire_style(card, chosen))


def _wire_style(card: dict[str, Any], endpoint: dict[str, str]) -> str:
    version = (endpoint.get("protocolVersion") or card.get("protocolVersion") or "").strip()
    if version.startswith("1"):
        return "v1"
    if version.startswith("0"):
        return "v03"
    # No version: the presence of supportedInterfaces is the best signal we have.
    if isinstance(card.get("supportedInterfaces"), list) and card.get("supportedInterfaces"):
        return "v1"
    return "v03"


# -------------------------------------------------------------------- identity


def name_key(value: Any) -> str:
    """The canonical form of a card name — the registry's notion of identity.

    An entry's ``id`` is derived from its name, so two cards that a human reads as
    "the same agent" must produce the same key. Leading and trailing whitespace is
    dropped, runs of whitespace between words collapse to one space, and the whole
    string is case-folded (``str.casefold`` also folds ``ß`` to ``ss`` and
    full-width letters to their narrow forms).

    Only the *key* is canonical: the card's ``name`` is never rewritten, so the
    panel keeps showing exactly the string the agent published.
    """
    if not isinstance(value, str):
        raise ValueError("an agent name must be a string")
    key = " ".join(value.split()).casefold()
    if not key:
        raise ValueError("an agent name must not be empty")
    return key


def derive_id(name: str) -> str:
    """``a_`` + the first 16 hex characters of ``sha256(name key)``.

    Identity is the name, not the URL: an agent that goes offline and comes back on
    a different host, port or scheme is the same agent and keeps its conversations,
    while one that renames itself is a different agent.
    """
    return "a_" + hashlib.sha256(name_key(name).encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------- facets


def card_summary(card: dict[str, Any]) -> dict[str, Any]:
    """The four search facets ADP builds from a card, with safe defaults."""
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    skill_ids: list[str] = []
    tags: list[str] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        if _nonempty_str(skill.get("id")):
            skill_ids.append(skill["id"].strip())
        raw_tags = skill.get("tags")
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                if isinstance(tag, str) and tag not in tags:
                    tags.append(tag)
    return {
        "name": str(card.get("name") or ""),
        "description": str(card.get("description") or ""),
        "skills": skill_ids,
        "tags": tags,
    }


# ----------------------------------------------------------------------- states


def normalize_state(value: Any) -> str:
    """Map any A2A task-state spelling onto one lower-case, dashed token.

    Handles proto JSON (``TASK_STATE_INPUT_REQUIRED``), proto names
    (``INPUT_REQUIRED``) and the 0.3 wire form (``input-required``).
    """
    text = str(value or "").strip()
    if not text:
        return "unknown"
    if text.upper().startswith("TASK_STATE_"):
        text = text[len("TASK_STATE_"):]
    text = text.strip().lower().replace("_", "-")
    if text in {"", "unspecified", "state-unspecified"}:
        return "unknown"
    return text


def is_terminal(state: str) -> bool:
    """True when the task will not change state on its own anymore."""
    return normalize_state(state) in TERMINAL_STATES


# ---------------------------------------------------------------------- results


def part_text(part: Any) -> str | None:
    """Render one A2A part as text, or None when it carries no rendering."""
    if not isinstance(part, dict):
        return None
    text = part.get("text")
    if isinstance(text, str):
        return text
    data = part.get("data")
    if data is not None:
        import json

        try:
            return json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(data)
    url = part.get("url")
    if isinstance(url, str):
        return url
    return None


def parts_text(parts: Iterable[Any]) -> str:
    """Join the textual rendering of several parts."""
    rendered = [text for text in (part_text(part) for part in parts) if text]
    return "\n\n".join(rendered)


def message_text(message: Any) -> str:
    """Extract text from an A2A Message object."""
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    return parts_text(parts if isinstance(parts, list) else [])


def interpret_result(result: Any) -> dict[str, Any]:
    """Normalize a ``SendMessage``/``GetTask`` result into the panel's view.

    A2A allows a direct ``Message`` reply instead of a ``Task``; both are turned
    into the same dict so storage and the UI only ever deal with one shape.
    """
    if not isinstance(result, dict):
        return {
            "kind": "unknown",
            "taskId": None,
            "contextId": None,
            "state": "unknown",
            "statusText": "",
            "text": "",
            "raw": result,
        }

    if "status" in result and "id" in result:
        status = result.get("status") if isinstance(result.get("status"), dict) else {}
        state = normalize_state(status.get("state"))
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
        artifact_text = "\n\n".join(
            parts_text(artifact.get("parts") or [])
            for artifact in artifacts
            if isinstance(artifact, dict)
        ).strip()
        return {
            "kind": "task",
            "taskId": str(result.get("id") or "") or None,
            "contextId": str(result.get("contextId") or "") or None,
            "state": state,
            "statusText": message_text(status.get("message")),
            "text": artifact_text,
            "artifactCount": len(artifacts),
            "history": result.get("history") if isinstance(result.get("history"), list) else [],
            "raw": result,
        }

    if "parts" in result or "role" in result:
        return {
            "kind": "message",
            "taskId": None,
            "contextId": str(result.get("contextId") or "") or None,
            "state": "message",
            "statusText": "",
            "text": message_text(result),
            "history": [result],
            "raw": result,
        }

    return {
        "kind": "unknown",
        "taskId": None,
        "contextId": None,
        "state": "unknown",
        "statusText": "",
        "text": "",
        "raw": result,
    }

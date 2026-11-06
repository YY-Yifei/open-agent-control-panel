"""RFC 8785 (JCS) canonical JSON and the semantic card digest.

``cardHash`` has to be reproducible by any consumer, so the canonicalization is
the same one A2A signatures use: keys sorted by UTF-16 code units, no
insignificant whitespace, and ECMAScript number formatting.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

CARD_HASH_PREFIX = "sha256:"


def _utf16_key(name: str) -> bytes:
    """JCS orders object keys by UTF-16 code units, not by code points."""
    return name.encode("utf-16-be")


def _number(value: float) -> str:
    """Approximate ECMAScript ``Number::toString`` for the values JSON allows."""
    if value != value or math.isinf(value):
        raise ValueError("JCS does not allow NaN or Infinity")
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    match = re.fullmatch(r"(-?)(\d+\.\d+|\d+)[eE]([+-]?\d+)", text)
    if match:
        sign, mantissa, exponent = match.groups()
        return f"{sign}{mantissa}e{'+' if int(exponent) > 0 else '-'}{abs(int(exponent))}"
    return text


def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = sorted((str(key) for key in value), key=_utf16_key)
        pairs = ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical(value[key])
            for key in keys
        )
        return "{" + pairs + "}"
    raise TypeError(f"cannot canonicalize {type(value)!r}")


def canonical_json(value: Any) -> str:
    """Return the RFC 8785 canonical text for a JSON value."""
    return _canonical(value)


def card_hash(card: Mapping[str, Any]) -> str:
    """``sha256:`` + hex of ``sha256(JCS(card))`` — a semantic digest."""
    digest = hashlib.sha256(canonical_json(card).encode("utf-8")).hexdigest()
    return CARD_HASH_PREFIX + digest

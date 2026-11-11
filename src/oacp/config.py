"""Runtime configuration, assembled from defaults, environment and CLI flags.

Environment variables use the ``OACP_`` prefix; every one of them is optional.
The CLI (``oacp serve``) turns its flags into a partial mapping that overrides
the environment, so precedence is simply: default < environment < flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import __version__

ENV_PREFIX = "OACP_"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}

CARD_MODES = {"strict", "relaxed"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    raise ValueError(f"not a boolean: {value!r}")


@dataclass(slots=True)
class Settings:
    """Everything the panel needs to know about its deployment."""

    # Storage
    db_path: str = "oacp.db"

    # Identity of this registry
    display_name: str = "Open Agent Control Panel"
    public_url: str = "http://127.0.0.1:8000"
    spec_url: str = "https://github.com/YY-Yifei/a2a-discover/blob/main/spec/adp-core.md"
    contact: str = ""

    # Panel API protection: when set, /api/panel/* requires the X-Panel-Token
    # header (or an equivalent bearer token).
    panel_token: str = ""

    # AgentCard validation: "strict" follows ADP Core §6.3 exactly, "relaxed"
    # also accepts A2A 0.3 cards that carry a plain ``url``.
    card_validation: str = "relaxed"

    # Probing
    probe_interval: int = 300
    down_probe_interval: int = 60
    probe_concurrency: int = 4
    probe_enabled: bool = True

    # Registration limits (ADP Core §5.6)
    max_card_bytes: int = 1_048_576
    max_agents_per_domain: int = 100
    register_per_hour: int = 20

    # Local development only: accept http:// (and private addresses) for card
    # URLs. The registry is no longer ADP-compliant with this switched on.
    allow_insecure_cards: bool = False

    # Outbound A2A calls
    allow_private_agents: bool = False
    a2a_timeout: float = 60.0

    # Images a user may send. The bytes travel inline in the A2A message (see
    # ``a2a.client``), so this is a *wire* limit as much as a storage one: it is
    # what keeps one JSON-RPC body inside what an agent will accept. Counted per
    # message, not per conversation.
    max_image_bytes: int = 5 * 1024 * 1024
    max_images_per_message: int = 4

    # Images an *agent* sends back. Separate knobs on purpose: what the panel
    # accepts from a person it vouches for and what it accepts from a remote
    # service are two different trust decisions, and only one of them is a
    # person watching their own file be rejected.
    max_inbound_image_bytes: int = 5 * 1024 * 1024
    max_inbound_images: int = 4

    # Whether the panel will GET a URL an agent names in a file part. On by
    # default because that is how the picture gets shown; off for a deployment
    # that must not act as a proxy for remote content. Switched off, a URL-shaped
    # part becomes a chip saying it was not fetched — the reply still arrives.
    fetch_inbound_files: bool = True

    # Inbound A2A push notifications (task callbacks). On by default: it is the
    # only protocol-defined way an agent can report a result without being asked,
    # and the callback base follows the bind address the same way local mode does,
    # so a loopback deployment already offers an address local agents can reach.
    # Switch it off (``--no-push``) when the panel cannot offer a reachable one.
    push_enabled: bool = True

    # Largest callback body accepted. Above the default because a callback that
    # carries an inline image is a normal A2A answer, and refusing it would mean
    # the panel loses whole turns from agents that answer with a picture.
    max_push_bytes: int = 8 * 1024 * 1024

    user_agent: str = f"open-agent-control-panel/{__version__}"

    def __post_init__(self) -> None:
        mode = str(self.card_validation or "").strip().lower()
        if mode not in CARD_MODES:
            raise ValueError(f"card_validation must be one of {sorted(CARD_MODES)}")
        self.card_validation = mode
        if not str(self.public_url).startswith(("http://", "https://")):
            raise ValueError("public_url must be an absolute http(s) URL")
        if self.probe_interval < 5:
            raise ValueError("probe_interval must be at least 5 seconds")
        if self.a2a_timeout <= 0:
            raise ValueError("a2a_timeout must be positive")

    # ------------------------------------------------------------------ derived

    @property
    def local_mode(self) -> bool:
        """True when card fetching was relaxed for a loopback deployment.

        This is the single fact the UI and the startup banner need: with it on,
        the registry accepts ``http`` card URLs and private/loopback targets, so
        it is no longer an ADP Core compliant deployment.
        """
        return self.allow_insecure_cards

    @property
    def api_base(self) -> str:
        """Base URL advertised in the RegistryCard, always ending in ``/v1``."""
        return self.public_url.rstrip("/") + "/v1"

    @property
    def push_callback_base(self) -> str:
        """Prefix the per-task push URL is built from (``/a2a/push/<token>``).

        It is derived from ``public_url`` because that is the one address the
        operator has already promised is reachable from the outside.
        """
        return self.public_url.rstrip("/") + "/a2a/push"

    def registry_card(self) -> dict[str, Any]:
        """The RegistryCard served from ``/.well-known/a2a-registry.json``."""
        card: dict[str, Any] = {
            "name": self.display_name,
            "apiBase": self.api_base,
            "spec": self.spec_url,
            "probeIntervalSeconds": self.probe_interval,
            "limits": {
                "maxCardBytes": self.max_card_bytes,
                "maxAgentsPerDomain": self.max_agents_per_domain,
            },
        }
        if self.contact:
            card["contact"] = self.contact
        return card

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_env(cls, overrides: Mapping[str, Any] | None = None) -> "Settings":
        """Build settings from ``OACP_*`` environment variables and overrides."""
        values: dict[str, Any] = {}
        for field in fields(cls):
            raw = os.environ.get(ENV_PREFIX + field.name.upper())
            if raw is not None:
                values[field.name] = _coerce(field.name, raw)
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)

    @property
    def public_origin(self) -> str:
        """Scheme and host of ``public_url`` (no trailing slash)."""
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"


def _coerce(name: str, raw: str) -> Any:
    if name in {
        "probe_interval",
        "down_probe_interval",
        "probe_concurrency",
        "max_card_bytes",
        "max_agents_per_domain",
        "register_per_hour",
        "max_image_bytes",
        "max_images_per_message",
        "max_inbound_image_bytes",
        "max_inbound_images",
        "max_push_bytes",
    }:
        return int(raw)
    if name == "a2a_timeout":
        return float(raw)
    if name in {
        "allow_private_agents",
        "allow_insecure_cards",
        "probe_enabled",
        "push_enabled",
        "fetch_inbound_files",
    }:
        return _as_bool(raw)
    return raw

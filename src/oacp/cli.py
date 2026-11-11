"""Command line interface for the control panel."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from typing import Sequence
from urllib.parse import urlsplit

from . import __version__
from .config import AGENT_POLICIES, Settings

LOG_LEVELS = ["critical", "error", "warning", "info", "debug", "trace"]


def build_parser() -> argparse.ArgumentParser:
    """The ``oacp`` command line."""
    parser = argparse.ArgumentParser(
        prog="oacp",
        description="Open Agent Control Panel — a registry and control plane for A2A agents.",
    )
    parser.add_argument("--version", action="version", version=f"open-agent-control-panel {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the control panel")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    serve.add_argument("--db", dest="db_path", help="SQLite file (default: oacp.db)")
    serve.add_argument(
        "--public-url",
        dest="public_url",
        help="externally visible base URL; advertised in the RegistryCard",
    )
    serve.add_argument("--name", dest="display_name", help="registry display name")
    serve.add_argument("--contact", help="contact line advertised in the RegistryCard")
    serve.add_argument(
        "--panel-token",
        dest="panel_token",
        help="require this token on /api/panel/* (the web UI asks for it once)",
    )
    serve.add_argument(
        "--card-validation",
        choices=["strict", "relaxed"],
        help="AgentCard validation mode (default: relaxed)",
    )
    serve.add_argument(
        "--allow-private-agents",
        action="store_true",
        default=None,
        help="allow A2A calls to private addresses (local development only)",
    )
    serve.add_argument(
        "--allow-insecure-cards",
        action="store_true",
        default=None,
        help="development only: accept http/private card URLs (breaks ADP Core compliance)",
    )
    serve.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="never relax https/private-address checks, even when bound to loopback",
    )
    serve.add_argument("--no-probe", action="store_true", default=None, help="disable background probing")
    serve.add_argument(
        "--push",
        action="store_true",
        default=None,
        help="offer agents a callback URL for task updates (on by default)",
    )
    serve.add_argument(
        "--no-push",
        dest="no_push",
        action="store_true",
        default=None,
        help="stop offering callback URLs; tasks are then only updated when asked",
    )
    serve.add_argument(
        "--aaas-url",
        dest="aaas_url",
        help="aaas server the built-in assistant connects to (default: ws://localhost:8888)",
    )
    serve.add_argument(
        "--aaas-token",
        dest="aaas_token",
        help="bearer token for that server; the panel then proxies the socket so the"
        " token never reaches the browser",
    )
    serve.add_argument(
        "--agent-policy",
        dest="agent_policy",
        choices=sorted(AGENT_POLICIES),
        help="how much of the panel the assistant may touch (default: act)",
    )
    serve.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_true",
        default=None,
        help="switch the built-in assistant off entirely",
    )
    serve.add_argument("--probe-interval", type=int, dest="probe_interval", help="probe interval in seconds")
    serve.add_argument("--a2a-timeout", type=float, dest="a2a_timeout", help="A2A request timeout in seconds")
    serve.add_argument("--log-level", default="info", choices=LOG_LEVELS, help="log level (default: info)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``oacp`` and ``open-agent-control-panel`` scripts."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command != "serve":
        parser.print_help()
        return 0

    overrides = {
        "db_path": args.db_path,
        "public_url": args.public_url,
        "display_name": args.display_name,
        "contact": args.contact,
        "panel_token": args.panel_token,
        "card_validation": args.card_validation,
        "allow_private_agents": True if args.allow_private_agents else None,
        "allow_insecure_cards": True if args.allow_insecure_cards else None,
        "probe_enabled": False if args.no_probe else None,
        "probe_interval": args.probe_interval,
        "a2a_timeout": args.a2a_timeout,
        "push_enabled": _push_override(args),
        "aaas_url": args.aaas_url,
        "aaas_token": args.aaas_token,
        "agent_policy": args.agent_policy,
        "agent_enabled": False if args.no_agent else None,
    }

    # A loopback bind is reachable only from this machine, so the SSRF battery
    # that guards outbound card fetching has no attacker left to stop: a local
    # process can already reach the same addresses without going through us.
    # Relaxing it by default is therefore the honest default, not a shortcut —
    # but it is *derived* from the bind address, never assumed: anything the
    # operator pinned (flag or environment) wins, and --strict opts out.
    overrides.update(_local_mode_overrides(args))

    try:
        settings = Settings.from_env(overrides)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    # When neither the flag nor the environment pins the public URL, derive it
    # from the bind address so the RegistryCard points somewhere reachable.
    if args.public_url is None and "OACP_PUBLIC_URL" not in os.environ:
        settings.public_url = _derived_public_url(args)

    logging.basicConfig(
        level=_log_level(args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    import uvicorn

    from .app import create_app

    application = create_app(settings)
    print(f"Open Agent Control Panel {__version__} — http://{args.host}:{args.port}")
    print(f"  registry API : {settings.api_base}")
    print(f"  database     : {settings.db_path}")
    for line in _push_banner(settings, args.host):
        print(line)
    for line in _agent_banner(settings):
        print(line)
    if settings.local_mode:
        print(
            "  local mode   : loopback bind — https and private-address checks are relaxed;"
            " ADP Core compliance is not claimed (use --strict to restore)"
        )
    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        log_level=args.log_level if args.log_level != "trace" else "debug",
        access_log=False,
    )
    return 0


def _push_override(args: argparse.Namespace) -> bool | None:
    """Push is on unless switched off; ``--no-push`` wins over a stale ``--push``."""
    if getattr(args, "no_push", False):
        return False
    if getattr(args, "push", False):
        return True
    return None


def _derived_public_url(args: argparse.Namespace) -> str:
    """The address this deployment advertises: bind host plus port.

    A wildcard bind is not an address anyone can dial, so it falls back to
    loopback. Everything derived from this — the RegistryCard, the push callback
    base — follows the same rule, which is why a loopback deployment gets
    ``http://localhost:PORT/a2a/push`` for free.
    """
    visible_host = args.host if args.host not in {"0.0.0.0", "::"} else "127.0.0.1"
    return f"http://{visible_host}:{args.port}"


def _push_banner(settings: Settings, bind_host: str) -> list[str]:
    """Lines that say where callbacks point, and when that address cannot work.

    Push notifications are on by default, so the banner has to answer the one
    question the flag used to answer implicitly: *is this URL reachable by the
    agents I am calling?* A loopback callback base on a wildcard (or public) bind
    is not — that is a deployment mistake worth printing, not a crash.
    """
    if not settings.push_enabled:
        return ["  callbacks    : off (--no-push) — tasks update only when asked"]
    base = settings.push_callback_base
    host = urlsplit(base).hostname or ""
    if _is_loopback_host(host) and not _is_loopback_host(bind_host):
        return [
            f"  callbacks    : {base}",
            "                 loopback address — only agents on this machine can call back;"
            " pass --public-url to offer a reachable one",
        ]
    return [f"  callbacks    : {base}"]


def _agent_banner(settings: Settings) -> list[str]:
    """Where the assistant connects, and when that address is a bad idea.

    A cleartext ``ws://`` to anything but loopback puts every panel query the
    model makes on the wire — worth one line, not a refusal: the operator may be
    on a trusted network and knows the trade-off.
    """
    if not settings.agent_enabled:
        return ["  assistant    : off (--no-agent)"]
    parts = urlsplit(settings.aaas_url)
    suffix = " via same-origin proxy" if settings.aaas_token else ""
    line = f"  assistant    : {settings.aaas_url} ({settings.agent_policy}){suffix}"
    if parts.scheme == "ws" and not _is_loopback_host(parts.hostname or ""):
        return [
            line,
            "                 cleartext ws:// to a non-loopback host — every panel"
            " query travels in the clear; use wss:// if that matters",
        ]
    return [line]


def _local_mode_overrides(args: argparse.Namespace) -> dict[str, bool]:
    """Relax the card-fetch guard when the panel is bound to loopback only.

    Returns only the keys the default decides; anything the operator pinned
    (flag or environment) is left alone, and ``--strict`` disables the default.
    """
    if getattr(args, "strict", False) or not _is_loopback_host(getattr(args, "host", "")):
        return {}
    relaxed: dict[str, bool] = {}
    if args.allow_insecure_cards is None and "OACP_ALLOW_INSECURE_CARDS" not in os.environ:
        relaxed["allow_insecure_cards"] = True
    if args.allow_private_agents is None and "OACP_ALLOW_PRIVATE_AGENTS" not in os.environ:
        relaxed["allow_private_agents"] = True
    return relaxed


def _is_loopback_host(host: str) -> bool:
    """True for bind addresses only this machine can reach."""
    text = (host or "").strip().strip("[]").lower()
    if text in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _log_level(name: str) -> int:
    return {
        "critical": logging.CRITICAL,
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "debug": logging.DEBUG,
        "trace": logging.DEBUG,
    }[name]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

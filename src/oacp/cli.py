"""Command line interface for the control panel."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Sequence

from . import __version__
from .config import Settings

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
    serve.add_argument("--no-probe", action="store_true", default=None, help="disable background probing")
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
    }

    try:
        settings = Settings.from_env(overrides)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    # When neither the flag nor the environment pins the public URL, derive it
    # from the bind address so the RegistryCard points somewhere reachable.
    if args.public_url is None and "OACP_PUBLIC_URL" not in os.environ:
        visible_host = args.host if args.host not in {"0.0.0.0", "::"} else "127.0.0.1"
        settings.public_url = f"http://{visible_host}:{args.port}"

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
    if settings.allow_private_agents:
        print("  warning      : private agent addresses are allowed (development mode)")
    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        log_level=args.log_level if args.log_level != "trace" else "debug",
        access_log=False,
    )
    return 0


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

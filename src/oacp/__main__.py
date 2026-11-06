"""Allow ``python -m oacp`` to behave like the ``oacp`` script."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

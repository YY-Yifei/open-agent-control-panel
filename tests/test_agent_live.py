"""The JS caller against a real ``aaas.Server`` — two implementations, one protocol.

The page owns the socket, so the protocol has a second implementation in
JavaScript and the Python one is the definition. Every other JS test uses a
hand-written socket, which cannot catch the one failure that matters most: that
the frames we write are the frames a real server reads.

Skipped when ``aaas`` is not installed — it is an optional extra, and a test suite
that fails on a missing optional dependency teaches people to ignore failures.
Install it with ``pip install -e ".[agent]"`` to run this.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

aaas = pytest.importorskip("aaas", reason="the live cross-language check needs the aaas extra")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "js" / "live_client.mjs"


async def _run() -> tuple[int, str, str]:
    """Serve one scripted turn, point the real client at it, report what it said."""

    async def handler(ctx) -> None:
        # The shape a model-driven turn would take: say something, call a
        # capability, then finish with the result it got back.
        ctx.delta("好的，")
        result = await ctx.call("ui_theme", {"theme": "dark"})
        ctx.reply(f"theme {result.status}")

    server = aaas.Server(handler)
    await server.start("127.0.0.1", 0)
    process = None
    try:
        url = f"ws://127.0.0.1:{server.port}"
        process = await asyncio.create_subprocess_exec(
            "node",
            str(SCRIPT),
            url,
            cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        return (
            process.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await server.close()


def test_the_page_client_speaks_the_real_protocol() -> None:
    """Handshake, declare, a turn, and a function call the server actually made."""
    if not SCRIPT.exists():
        pytest.skip(f"missing {SCRIPT}")

    try:
        code, out, err = asyncio.run(_run())
    except FileNotFoundError:  # pragma: no cover - node is not part of the runtime
        pytest.skip("node is not on PATH")

    assert code == 0, f"exit {code}\nstdout:\n{out}\nstderr:\n{err}"
    assert "ok" in out


def test_the_live_script_fails_loudly_when_a_claim_breaks() -> None:
    """A script that only reports success would pass by connecting and nothing else.

    This guards the guard: the live check must collect failures and exit non-zero,
    rather than printing "ok" unconditionally at the end.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    for claim in ("sessionId", "historyMode", "limits", "declare", "turnStatus", "ui_theme"):
        assert claim in source, claim
    assert "if (failures.length)" in source
    assert "process.exit(1)" in source
    # Success is the *last* thing after the failures are reported, not the default.
    assert source.index("process.exit(1)") < source.index('console.log("ok")')

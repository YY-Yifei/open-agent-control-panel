"""Background probing: the only thing that keeps ``lastOkAt`` honest."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .service import RegistryService

log = logging.getLogger(__name__)


class ProbeLoop:
    """Periodically re-fetches registered cards.

    Failures are recorded, not raised: a probe loop that dies on the first
    unreachable agent would quietly stop refreshing every other entry.
    """

    def __init__(
        self,
        service: RegistryService,
        *,
        tick_seconds: float = 5.0,
        concurrency: int = 4,
    ) -> None:
        self.service = service
        self.tick_seconds = max(1.0, tick_seconds)
        self.concurrency = max(1, concurrency)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the loop on a daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="oacp-probe", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the loop to stop and wait briefly for the current tick."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    # ----------------------------------------------------------------- internals

    def _run(self) -> None:
        log.info("probe loop started (tick=%ss, concurrency=%s)", self.tick_seconds, self.concurrency)
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception:  # pragma: no cover - defensive: never kill the loop
                log.exception("probe tick failed")
        log.info("probe loop stopped")

    def tick(self) -> int:
        """Probe the agents that are due; returns how many were visited."""
        due = self.service.due_agents(self.concurrency)
        if not due:
            return 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            list(pool.map(self._probe_one, due))
        return len(due)

    def _probe_one(self, row: dict) -> None:
        try:
            self.service.probe(row["id"])
        except Exception:  # pragma: no cover - defensive
            log.exception("probe failed for %s", row.get("id"))

"""Process lifecycle for packaged installs: health proof, shutdown request, self-exit.

The Windows launcher starts the server with a per-launch ``CONTROL_SECRET`` and
``EXIT_WHEN_UNUSED_SECONDS``. Nothing here is active in the owner's git checkout, where
both are unset.

Exit codes a supervisor (launch.pyw) understands: 0 = stopped normally (shutdown request or
unused), 75 = restart me (re-read state.json; used by updates). ``app.state.request_exit``
is replaced by run_packaged.py with a function that stops uvicorn; the default only logs.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import threading
from collections.abc import Callable

log = logging.getLogger("fintrack.lifecycle")

EXIT_OK = 0
EXIT_RESTART = 75  # EX_TEMPFAIL: "run me again" (the launcher re-reads state.json)

EXIT_WATCH_TICK_SECONDS = 30

HEALTH_PATH = "/api/health"
SHUTDOWN_PATH = "/api/app/shutdown"

PROOF_DOMAIN = b"fintrack-health-v1"


def health_proof(secret: str, nonce: str, boot_id: str) -> str:
    """HMAC-SHA256 the launcher recomputes: proves the answer comes from the server it
    started (not a program squatting on the port). Bound to this boot."""
    message = b"\n".join((PROOF_DOMAIN, nonce.encode("ascii"), boot_id.encode("ascii")))
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def secret_matches(expected: str, given: str | None) -> bool:
    if not expected or given is None:
        return False
    return hmac.compare_digest(
        expected.encode("utf-8", "surrogatepass"), given.encode("utf-8", "surrogatepass")
    )


class ExitWatcher:
    """Heartbeat-miss self-exit, counted in ticks so it is sleep-safe.

    The heartbeat is ``POST /api/app/alive`` (routers/app.py), which every FinTrack window
    sends every 30 s, locked or not; no other request counts.

    Every ``tick()`` (one per EXIT_WATCH_TICK_SECONDS from a loop) either resets the miss
    count, when something was heard since the last tick or a busy check says "keep running"
    (the vault is unlocked), or adds one miss. After ``limit_seconds / tick_seconds`` misses in
    a row it asks for exit code 0, once. Counting ticks rather than comparing wall-clock
    times means a computer waking from sleep sees one missed tick, not hours of silence.
    """

    def __init__(
        self,
        limit_seconds: int,
        request_exit: Callable[[int], None],
        busy_checks: list[Callable[[], bool]] | None = None,
        tick_seconds: int = EXIT_WATCH_TICK_SECONDS,
    ) -> None:
        self.tick_seconds = tick_seconds
        self.max_misses = math.ceil(limit_seconds / tick_seconds) if limit_seconds > 0 else 0
        self._request_exit = request_exit
        self.busy_checks: list[Callable[[], bool]] = list(busy_checks or [])
        self._lock = threading.Lock()
        self._heard = False
        self.misses = 0
        self.fired = False

    @property
    def enabled(self) -> bool:
        return self.max_misses > 0

    def beat(self) -> None:
        with self._lock:
            self._heard = True

    def _busy(self) -> bool:
        for check in self.busy_checks:
            try:
                if check():
                    return True
            except Exception as exc:  # noqa: BLE001 - a failing check must not stop the server
                log.warning("exit-watch busy check failed: %s", type(exc).__name__)
                return True
        return False

    def tick(self) -> bool:
        """One tick; True when this tick asked the server to exit."""
        if not self.enabled or self.fired:
            return False
        with self._lock:
            heard, self._heard = self._heard, False
        if heard or self._busy():
            self.misses = 0
            return False
        self.misses += 1
        if self.misses < self.max_misses:
            return False
        self.fired = True
        log.info("no FinTrack window for %d checks; stopping", self.misses)
        self._request_exit(EXIT_OK)
        return True

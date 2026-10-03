"""Which app windows (browser tabs) are open, for the App-window states (D4).

Every page sends a random tab id (``X-FinTrack-Tab``, ``crypto.randomUUID()`` kept in its
sessionStorage). The id is never an auth factor: it only says *which* window is talking, so
the server can tell "Iron Owl is already open in another window" from "locked", bind a
session to the window that opened it, and lock when that window closes.

- ``seen(tab)``: from ``GET /api/auth/status`` and ``POST /api/app/alive`` (every 30 s).
  Also cancels that tab's pending close (a reload: pagehide → closing → status again).
- ``request_close(tab, since)``: ``POST /api/app/closing`` from the bound tab. The vault
  locks (reason ``closed``) once CLOSE_GRACE seconds pass without that tab coming back
  (ignored when the tab was heard from after the request arrived).
- ``present(tab)``: seen within PRESENCE_TTL and not closing. ``window_open()``: any.

At most MAX_TABS ids are kept (oldest dropped first, but never the window holding the
session or one whose close is pending). Own lock, never held with the vault's.
Times come from ``security.now`` (monotonic; tests patch it).
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict

from . import security

TAB_HEADER = "X-FinTrack-Tab"
TAB_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
PRESENCE_TTL = 100  # seconds: a window heartbeats every 30 s, so 3 missed beats and a margin
CLOSE_GRACE = 8  # seconds between "this window is closing" and the lock (F5 comes back sooner)
MAX_TABS = 32


def valid_tab(value: str | None) -> str | None:
    """The tab id if well formed, else None."""
    if value is None or not TAB_ID_RE.fullmatch(value):
        return None
    return value


class Presence:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seen: OrderedDict[str, float] = OrderedDict()
        self._close_at: dict[str, float] = {}  # tab -> when its pending close is due

    def seen(self, tab: str, keep: str | None = None) -> None:
        """This window is open (status or heartbeat); cancels its pending close.

        ``keep``: the window holding the session (the caller passes the vault's bound tab).
        Past MAX_TABS the oldest *other* id is dropped: never ``keep``, never a window whose
        close is pending (its close-lock must still happen), never ``tab`` unless nothing
        else can go. So a flood of made-up ids can neither cancel a close-lock nor hide the
        open window from a real second one. Eviction never touches ``_close_at``.
        """
        with self._lock:
            self._last_seen[tab] = security.now()
            self._last_seen.move_to_end(tab)
            self._close_at.pop(tab, None)
            if len(self._last_seen) > MAX_TABS:
                self._evict(tab, keep)

    def _evict(self, tab: str, keep: str | None) -> None:
        while len(self._last_seen) > MAX_TABS:
            victim = next(
                (
                    t
                    for t in self._last_seen
                    if t != tab and t != keep and t not in self._close_at
                ),
                None,
            )
            if victim is None:
                # Only protected ids left: the newcomer isn't tracked (unless it's ``keep``).
                if tab != keep:
                    self._last_seen.pop(tab, None)
                return
            del self._last_seen[victim]

    def forget(self, tab: str) -> None:
        with self._lock:
            self._last_seen.pop(tab, None)
            self._close_at.pop(tab, None)

    def last_seen(self, tab: str) -> float | None:
        with self._lock:
            return self._last_seen.get(tab)

    def _present(self, tab: str, t: float) -> bool:
        seen = self._last_seen.get(tab)
        return seen is not None and t - seen <= PRESENCE_TTL and tab not in self._close_at

    def present(self, tab: str | None) -> bool:
        """Heard from within PRESENCE_TTL and not closing."""
        if tab is None:
            return False
        with self._lock:
            return self._present(tab, security.now())

    def window_open(self) -> bool:
        with self._lock:
            t = security.now()
            return any(self._present(tab, t) for tab in self._last_seen)

    def request_close(self, tab: str, since: float | None = None) -> bool:
        """Start the grace period; ``close_due`` says when it has run out.

        ``since``: when the closing request arrived. If the window was heard from after
        that (a reload's status overtook the old page's beacon), nothing happens. True
        when the grace started.
        """
        with self._lock:
            last = self._last_seen.get(tab)
            if since is not None and last is not None and last > since:
                return False
            self._close_at[tab] = security.now() + CLOSE_GRACE
            return True

    def closing(self, tab: str) -> bool:
        with self._lock:
            return tab in self._close_at

    def close_due(self, tab: str) -> bool:
        """True (once) when ``tab``'s close is pending and its grace has run out; the tab is
        then forgotten. False when it came back (or never asked)."""
        with self._lock:
            due = self._close_at.get(tab)
            if due is None or security.now() < due:
                return False
            self._close_at.pop(tab, None)
            self._last_seen.pop(tab, None)
            return True

    def due_closes(self) -> list[str]:
        """Every tab whose grace has run out (the periodic backstop sweep)."""
        with self._lock:
            t = security.now()
            return [tab for tab, due in self._close_at.items() if t >= due]

    def __len__(self) -> int:
        with self._lock:
            return len(self._last_seen)


def open_elsewhere(presence: Presence, view: security.PresenceView, tab: str | None) -> bool:
    """FinTrack is unlocked in another window that is still open, and the caller has no
    valid session of its own (it may hold one that moved there)."""
    bound = view.bound_tab
    return (
        view.vault_unlocked
        and view.state != security.SESSION_OK
        and bound is not None
        and bound != tab
        and presence.present(bound)
    )


def reported_lock_reason(view: security.PresenceView) -> str | None:
    """``moved`` for a window whose session another window took over; while the vault is
    unlocked (for the caller or someone else) null; else why it last locked (null = not
    since this server started, or unknown)."""
    if view.state == security.SESSION_MOVED:
        return "moved"
    if view.vault_unlocked:
        return None
    return view.lock_reason

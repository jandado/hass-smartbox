"""Feed (websocket) liveness, as its own state machine.

The store can be perfectly valid while the wire is silent, and then every value
in it is stale. This module owns that signal alone - the grace window an
unexpected close opens, why the feed went down, how long it has been down, and
the counters diagnostics renders - and emits
:class:`~smartbox_core.state.FeedChange`.

Ordering with the engine: :meth:`FeedMonitor.note_frame` RETURNS its changes so
the engine can append them after the frame's own (a frame that ends an outage
must not report the recovery before the data it carried); the window-expiry path
notifies through the injected callback from its own task.

Every close opens a window, scheduled or not: the vendor cuts the socket at the
presented token's expiry every ~4 h and the transport reconnects seconds later
(docs/api-contract.md §2.5, §4.1), so a healthy reconnect is cancelled by the
frame that follows it - while a reconnect that FAILS leaves the window to
expire. Sleeps go through the shared timing seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import logging
from typing import TYPE_CHECKING, Any

from ..smartbox_protocol import timing
from .state import Change, FeedChange

if TYPE_CHECKING:
    from ..smartbox_protocol.events import SocketClosed

__all__ = ["FEED_GRACE_SECONDS", "FeedMonitor"]

_LOGGER = logging.getLogger(__name__)

# Grace window before a silent feed flips entities Unavailable. A socket that
# closes and delivers a frame again inside it is not an outage: the vendor's
# scheduled token-expiry cut reconnects in a few seconds, and a transient
# network blip is indistinguishable from it at the socket level.
FEED_GRACE_SECONDS = 10.0

NotifyChanges = Callable[[Sequence[Change]], Awaitable[None]]


class FeedMonitor:
    """Tracks whether the wire is delivering, and reports each transition.

    ``notify`` is the engine's change pump: this object emits from a timer
    task of its own, so it cannot hand its changes back to a caller.
    """

    def __init__(
        self,
        notify: NotifyChanges,
        *,
        grace_s: float = FEED_GRACE_SECONDS,
    ) -> None:
        """Start with the feed assumed up and no window running."""
        self.grace_s = grace_s
        self._notify = notify
        self._available = True
        self._timer: asyncio.Task[None] | None = None
        self._down_since: float | None = None
        self._reason: str | None = None
        self._outages = 0
        self._last_outage_s: float | None = None
        self._last_close_expected: bool | None = None
        self._stopped = False

    @property
    def available(self) -> bool:
        """True while the feed is up (or inside its grace window)."""
        return self._available

    @property
    def window_pending(self) -> bool:
        """True while a grace window is running (no outage reported yet)."""
        return self._timer is not None

    @property
    def facts(self) -> dict[str, Any]:
        """Read-only feed facts for diagnostics (no behaviour change)."""
        down_s = (
            None
            if self._down_since is None
            else round(timing.monotonic() - self._down_since, 3)
        )
        return {
            "available": self._available,
            "reason": self._reason,
            "down_s": down_s,
            "outages": self._outages,
            "last_outage_s": self._last_outage_s,
            "last_close_expected": self._last_close_expected,
            "grace_s": self.grace_s,
        }

    async def on_socket_closed(self, event: SocketClosed) -> None:
        """Start the grace window for one ended connection.

        The window is armed for every close, the scheduled token-expiry cut
        included: the transport reconnects with a fresh token within seconds, so
        a frame inside the window clears it silently - while a reconnect that
        FAILS (the token endpoint 5xx-ing, the network gone) leaves the window
        to expire and reports the outage. An expected close that skipped the
        window would leave entities rendering frozen values as available for as
        long as the failure lasts, since no frame can arrive to correct them.
        """
        if self._stopped:
            return
        self._last_close_expected = event.expected
        if event.expected:
            _LOGGER.info(
                "smartbox socket cut at token expiry (code=%s); reconnecting "
                "with a fresh token",
                event.code,
            )
        elif self._timer is None and self._available:
            _LOGGER.debug(
                "smartbox socket closed (code=%s); %gs grace window started",
                event.code,
                self.grace_s,
            )
        if self._timer is not None or not self._available:
            return  # one window per outage
        reason = (
            f"websocket closed (code={event.code}) and no frame for "
            f"{self.grace_s:g}s"
        )
        self._timer = asyncio.create_task(self._watch(reason))

    def note_frame(self) -> list[Change]:
        """Register that the feed delivered a frame; report a recovery once."""
        changes: list[Change] = []
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
            _LOGGER.debug("smartbox feed answered inside the grace window")
        if not self._available:
            outage_s = (
                None
                if self._down_since is None
                else round(timing.monotonic() - self._down_since, 3)
            )
            self._available = True
            self._down_since = None
            self._last_outage_s = outage_s
            self._reason = None
            _LOGGER.info(
                "smartbox feed restored%s",
                "" if outage_s is None else f" after {outage_s:.1f}s",
            )
            changes.append(FeedChange(available=True, outage_s=outage_s))
        return changes

    async def mark_down(self, reason: str) -> None:
        """Report the feed down now (a stopped transport pushes no frames)."""
        await self._notify(self._down(reason))

    def stop(self) -> None:
        """Cancel a pending window; the feed never flips afterwards."""
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def start(self) -> None:
        """Re-arm the monitor for a new supervision run.

        ``stop()`` is terminal for the run it ended, but the engine supports
        being supervised again: without this reset the feed could never report
        an outage again.
        """
        self._stopped = False

    async def _watch(self, reason: str) -> None:
        """Flip the feed down when the grace window expires unfed."""
        try:
            await timing.sleep(self.grace_s)
        except asyncio.CancelledError:
            return
        self._timer = None
        if self._stopped:
            return
        await self._notify(self._down(reason))

    def _down(self, reason: str) -> list[Change]:
        """Mark the feed down and build its change (once per outage)."""
        if not self._available:
            return []
        self._available = False
        self._down_since = timing.monotonic()
        self._reason = reason
        self._outages += 1
        # One line per outage, not one per closed socket.
        _LOGGER.warning("smartbox feed down: %s", reason)
        return [FeedChange(available=False, reason=reason)]

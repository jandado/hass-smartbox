"""Transport seam: the only way frames reach the core layer.

Contract (from docs/api-contract.md §4, the transport doctrine):

- One per-user ws_user connection is the only shipped transport; the seam
  exists so an alternative could sit behind the same interface.
- Liveness belongs to the transport: the client heartbeat detects the invisible
  half-open, data silence (up to ~13 min) is never a failure signal, and token
  expiry forces a fresh-token reconnect every 4 h (~4 s outage, close 1006).
- Reconnect uses a fresh token per connect and jittered ``1s * 2^n`` backoff
  capped at 30 s; transient failures are retried. The ladder that decides which
  outcome escalates (a short served cycle, an auth rejection) lives on
  ``WsUserTransport._between_cycles_pause``.
- Writes are REST, never socket messages, and live on the API client rather
  than in this seam.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .events import Event

EventHandler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True, repr=False)
class WsCredentials:
    """Credentials for one ws_user connect (fresh token per connect).

    ``repr=False`` like the other credential holders (``AuthConfig``,
    ``TokenBundle``): the generated repr would print a live access token the
    moment anything logged it.
    """

    token: str
    user_id: str


class TokenSource(ABC):
    """Credential seam; the transport only invalidates and re-acquires.

    ``acquire`` MUST return a fresh or valid token - the transport calls it
    before every connect. ``invalidate`` marks a rejected credential, so the
    next acquire forces a real refresh. An ``AuthError`` out of ``acquire`` is
    propagated by ``run()``: auth rejections are the only supervisor-visible
    stop.
    """

    @abstractmethod
    async def acquire(self) -> WsCredentials:
        """Return fresh credentials for the next connect."""

    @abstractmethod
    async def invalidate(self, rejected: WsCredentials) -> None:
        """Mark ``rejected`` unusable; force a refresh on next acquire."""


class Transport(ABC):
    """Frame pump; owns liveness and reconnect policy, never interprets frames."""

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> None:
        """Register the single async event handler (replaces any previous one)."""

    @abstractmethod
    async def run(self) -> None:
        """Connect and pump frames, reconnecting per policy.

        Parks until :meth:`close` or cancellation - mirrors the real
        socket's ``run()`` contract. Callers supervise this task.
        """

    @abstractmethod
    async def close(self) -> None:
        """Stop the pump and tear the socket down cleanly."""

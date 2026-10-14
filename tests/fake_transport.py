"""Reference fake transport: the double the transport tests build on.

Tests script frames in, drive ``run()`` exactly like the real transport,
and park it the same way - so a test that passes here exercises the same
seam contract the real socket satisfies.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from custom_components.smartbox.smartbox_protocol.events import Event, Snapshot, Update
from custom_components.smartbox.smartbox_protocol.transport import (
    EventHandler,
    Transport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any


class FakeTransport(Transport):
    """In-memory transport with test-side frame injection."""

    def __init__(self) -> None:
        """Set up an unconnected transport with no handler."""
        self._handler: EventHandler | None = None
        self._closed = asyncio.Event()
        self._deliveries: list[Event] = []

    def subscribe(self, handler: EventHandler) -> None:
        """Register the single async event handler (replaces any previous one)."""
        self._handler = handler

    async def run(self) -> None:
        """Park until :meth:`close` - the real socket's ``run()`` contract."""
        await self._closed.wait()

    async def close(self) -> None:
        """Unpark :meth:`run` and stop accepting deliveries."""
        self._closed.set()

    # --- test-side controls ---

    @property
    def deliveries(self) -> list[Event]:
        """Events delivered so far, in order."""
        return self._deliveries

    @property
    def closed(self) -> bool:
        """True once :meth:`close` was called."""
        return self._closed.is_set()

    @property
    def facts(self) -> dict[str, int | bool | str | None]:
        """Mirror the real transport's diagnostics facts shape."""
        return {
            "running": not self._closed.is_set(),
            "short_cycle_streak": 0,
            "auth_rejection_streak": 0,
            "last_close_code": None,
            "last_end_reason": None,
        }

    async def deliver(self, event: Event) -> None:
        """Push one event through the subscribed handler."""
        self._deliveries.append(event)
        if self._handler is not None:
            await self._handler(event)

    async def deliver_update(self, dev_id: str, path: str, body: object) -> None:
        """Shorthand: push one ``Update`` frame."""
        await self.deliver(Update(dev_id=dev_id, path=path, body=body))

    async def deliver_snapshot(self, payload: Sequence[Mapping[str, Any]]) -> None:
        """Shorthand: push one ``Snapshot`` frame."""
        await self.deliver(Snapshot(payload=payload))

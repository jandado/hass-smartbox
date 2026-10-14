"""Behavioral spec for the transport seam's reference fake.

These tests pin the contract the real ws_user transport must satisfy
(api-contract.md §4): a run() that parks, delivery of typed
events through the subscribed async handler.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from custom_components.smartbox.smartbox_protocol import (
    Event,
    Snapshot,
    SocketClosed,
    Update,
)
from tests.fake_transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def _collector() -> tuple[Callable[[Event], Awaitable[None]], list[Event]]:
    """Async handler contract, with the received events kept for asserts."""
    seen: list[Event] = []

    async def collect(event: Event) -> None:
        seen.append(event)

    return collect, seen


async def test_delivers_typed_update() -> None:
    transport = FakeTransport()
    collect, seen = _collector()
    transport.subscribe(collect)

    await transport.deliver_update(
        "dev-1", "/htr/1/status", {"mtemp": "19.5", "stemp": "20.0"}
    )

    assert len(seen) == 1
    event = seen[0]
    assert isinstance(event, Update)
    assert event.dev_id == "dev-1"
    assert event.path == "/htr/1/status"
    assert event.body["stemp"] == "20.0"
    assert transport.deliveries == seen


async def test_delivers_snapshot_and_closed() -> None:
    transport = FakeTransport()
    collect, seen = _collector()
    transport.subscribe(collect)

    await transport.deliver_snapshot({"homes": []})
    await transport.deliver(SocketClosed(code=1006))

    assert isinstance(seen[0], Snapshot)
    assert isinstance(seen[1], SocketClosed)
    assert seen[1].code == 1006


async def test_run_parks_until_close() -> None:
    transport = FakeTransport()
    task = asyncio.ensure_future(transport.run())
    await asyncio.sleep(0)
    assert not task.done()

    await transport.close()
    await asyncio.wait_for(task, timeout=1)
    assert task.done()
    assert transport.closed


async def test_subscribe_replaces_previous_handler() -> None:
    transport = FakeTransport()
    first_collect, first = _collector()
    second_collect, second = _collector()
    transport.subscribe(first_collect)
    transport.subscribe(second_collect)

    await transport.deliver_update("dev-1", "/connected", {"connected": True})

    assert first == []
    assert len(second) == 1

"""Typed events crossing the transport seam.

Payloads stay raw mappings and normalization belongs to values.py at the
protocol boundary, so core never inspects transport-specific frame shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class Snapshot:
    """Full homes/devs snapshot (the ws_user ``all_data`` reply).

    ``payload`` is the raw ``data`` value: homes -> devs -> devData.
    On the wire that value is ALWAYS a list of homes.
    """

    payload: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class Update:
    """One pushed change frame (ws_user ``update``).

    ``path`` is the wire path (``/htr/<addr>/status``-style) and ``body`` the
    raw pushed body. The body is an object for every frame except the box
    power-limit path, which may push the bare digit string
    (docs/api-ws-user.md §1.4); deciding what a body means is the core's job,
    not the transport's. Redundant frames are expected
    (docs/api-contract.md §6.2); dedup is the core store's job.
    """

    dev_id: str
    path: str
    body: object


@dataclass(frozen=True)
class SocketClosed:
    """The socket ended.

    ``code`` mirrors the WebSocket close code. On ws_user, 1006 covers both
    server-side deaths (the token ``exp`` cut) and locally-detected dead paths
    (heartbeat send failure); only timestamps and context tell them apart
    (docs/api-contract.md §4.1). Reconnect policy is the transport's, so core
    treats this as informational.

    ``expected`` marks the vendor's SCHEDULED cut - the server ends the socket
    exactly at the expiry of the token the connection was opened with - which
    core keeps out of the availability episodes.
    """

    code: int
    expected: bool = False


Event = Snapshot | Update | SocketClosed

"""First-setup consumption-history import, shared across one entry's nodes.

The 3-year import is a per-node job whose completion is recorded by a single
flag in the entry options. Two properties keep it honest:

- The flag flips only when EVERY node has imported. Flipping it from the first
  node to finish would leave the others reading a day of history on their next
  attempt, with the 3-year range never re-read.
- Flipping it must not reload the entry. The flag is entry-global state the
  sensors write themselves, and a reload tears down the websocket and every
  import still in flight.

This object owns both halves of that state for one config entry.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .const import CONF_HISTORY_CONSUMPTION, HistoryConsumptionStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["HistoryImport"]


class HistoryImport:
    """One entry's first-setup history import state."""

    def __init__(self, node_keys: Iterable[str], options: Mapping[str, Any]) -> None:
        """Track the nodes still owing an import and the applied options."""
        self._all: frozenset[str] = frozenset(node_keys)
        self._pending: set[str] = set(self._all)
        self._options: dict[str, Any] = dict(options)
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._last_error: dict[str, str] = {}

    @property
    def total_nodes(self) -> int:
        """Number of nodes whose first import the entry tracks."""
        return len(self._all)

    @property
    def pending_nodes(self) -> tuple[str, ...]:
        """Nodes whose first import has not completed, sorted for stable output."""
        return tuple(sorted(self._pending))

    @property
    def completed_nodes(self) -> tuple[str, ...]:
        """Nodes whose first import has completed, sorted for stable output."""
        return tuple(sorted(self._all - self._pending))

    def pending(self, node_key: str) -> bool:
        """Whether this node still owes its first-setup import."""
        return node_key in self._pending

    def complete(self, node_key: str) -> bool:
        """Mark one node imported; True when no node is left pending."""
        self._pending.discard(node_key)
        self.clear_failure(node_key)
        return not self._pending

    def failure_count(self, node_key: str) -> int:
        """Consecutive failed attempts for a node's first import."""
        return self._failures.get(node_key, 0)

    def retry_at(self, node_key: str) -> float:
        """Monotonic time of the next allowed attempt (0 = none armed)."""
        return self._retry_at.get(node_key, 0.0)

    def record_failure(self, node_key: str, retry_at: float, error: str) -> int:
        """Record one failed attempt and return the new consecutive count."""
        count = self._failures.get(node_key, 0) + 1
        self._failures[node_key] = count
        self._retry_at[node_key] = retry_at
        self._last_error[node_key] = error
        return count

    def clear_failure(self, node_key: str) -> None:
        """Forget a node's failure state (after a success or completion)."""
        self._failures.pop(node_key, None)
        self._retry_at.pop(node_key, None)
        self._last_error.pop(node_key, None)

    def diagnostics(self, now: float | None = None) -> dict[str, Any]:
        """Return the import ledger for config-entry diagnostics.

        ``retry_in_s`` is derived here so the payload shows how long the
        backoff still has to run; the stored deadlines stay monotonic.
        """
        current = time.monotonic() if now is None else now
        return {
            "history_consumption": self._options.get(CONF_HISTORY_CONSUMPTION),
            "total_nodes": self.total_nodes,
            "pending_nodes": list(self.pending_nodes),
            "completed_nodes": list(self.completed_nodes),
            "failures": {
                node_key: {
                    "count": count,
                    "last_error": self._last_error.get(node_key),
                    "retry_in_s": max(
                        0.0, round(self._retry_at.get(node_key, current) - current, 1)
                    ),
                }
                for node_key, count in sorted(self._failures.items())
            },
        }

    def takes_options(self, options: Mapping[str, Any]) -> bool:
        """Record the new options; True when a reload is needed to apply them.

        A change to the history flag alone is normally not reload-worthy: the
        sensors write that flag themselves once every node has imported, and
        reloading for it would cancel the imports of the nodes that have not
        finished. The one exception is a transition TO ``start``: that is the
        user asking for the first-setup import again, and the pending set is
        seeded when the runtime is built, so only a reload can honour it.
        """
        changed = {
            key: value
            for key, value in options.items()
            if self._options.get(key) != value
        }
        self._options = dict(options)
        if not changed:
            return False
        if set(changed) == {CONF_HISTORY_CONSUMPTION}:
            return bool(
                changed[CONF_HISTORY_CONSUMPTION] == HistoryConsumptionStatus.START
            )
        return True

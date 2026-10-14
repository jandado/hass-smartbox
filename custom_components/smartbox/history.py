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

from typing import TYPE_CHECKING, Any

from .const import CONF_HISTORY_CONSUMPTION, HistoryConsumptionStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["HistoryImport"]


class HistoryImport:
    """One entry's first-setup history import state."""

    def __init__(self, node_keys: Iterable[str], options: Mapping[str, Any]) -> None:
        """Track the nodes still owing an import and the applied options."""
        self._pending: set[str] = set(node_keys)
        self._options: dict[str, Any] = dict(options)

    def pending(self, node_key: str) -> bool:
        """Whether this node still owes its first-setup import."""
        return node_key in self._pending

    def complete(self, node_key: str) -> bool:
        """Mark one node imported; True when no node is left pending."""
        self._pending.discard(node_key)
        return not self._pending

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
            return (
                changed[CONF_HISTORY_CONSUMPTION]
                == HistoryConsumptionStatus.START
            )
        return True

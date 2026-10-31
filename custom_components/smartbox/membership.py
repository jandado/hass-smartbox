"""Membership watch: report nodes that appear after platform setup.

Entity creation happens once, when the platforms set up from the REST
discovery snapshot. The websocket snapshot is authoritative for membership
and can introduce a node later (a newly paired heater, for example). Its
state then exists in the engine store with no entities to render it.

This watcher keeps the setup-time membership and raises one repair issue per
entry when a fully parsed snapshot adds a node, asking for a reload. Removed
nodes are deliberately ignored: their entities stay in place and render
unavailable, so a backend glitch cannot delete history or unique ids.

``NodeAdded`` is the seam for adding entities in place later; until then the
repair issue is the complete user-facing behavior.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .smartbox_core.state import NodeAdded, node_key

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import SmartboxConfigEntry
    from .identity import SmartboxIdentity
    from .smartbox_core.engine import SmartboxEngine
    from .smartbox_core.state import Change

_LOGGER = logging.getLogger(__name__)

_NODE_ADDED_ISSUE = "node_added"
_MAX_LABELS = 10

__all__ = ["MembershipWatcher", "clear_node_added_issue", "node_added_issue_id"]


@callback
def node_added_issue_id(entry: SmartboxConfigEntry) -> str:
    """Per-entry repair issue id for nodes added after setup."""
    return f"{entry.entry_id}_{_NODE_ADDED_ISSUE}"


@callback
def clear_node_added_issue(hass: HomeAssistant, entry: SmartboxConfigEntry) -> None:
    """Clear the reload hint (a successful setup has materialized the nodes)."""
    ir.async_delete_issue(hass, DOMAIN, node_added_issue_id(entry))


def _format_labels(labels: list[str]) -> str:
    """Join node labels, bounding a pathological membership list.

    Truncation uses an ellipsis instead of an English "and N more" so the
    placeholder stays inside the translated sentence.
    """
    shown = ", ".join(labels[:_MAX_LABELS])
    if len(labels) > _MAX_LABELS:
        shown += ", …"
    return shown


class MembershipWatcher:
    """Turn ``NodeAdded`` events into one reload repair issue per entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SmartboxConfigEntry,
        engine: SmartboxEngine,
        identity: SmartboxIdentity,
    ) -> None:
        """Seed the known set from discovery and subscribe to the engine."""
        self._hass = hass
        self._entry = entry
        self._known: set[tuple[str, str]] = {
            (node.dev_id, node_key(node.node_type, node.addr))
            for node in identity.nodes
        }
        self._known_devices = {device.dev_id for device in identity.devices}
        self._new_nodes: dict[tuple[str, str], str] = {}
        engine.subscribe(self._on_change)

    async def _on_change(self, change: Change) -> None:
        """Record one newly reported node and refresh the repair issue."""
        if not isinstance(change, NodeAdded):
            return
        token = (change.dev_id, change.node_key)
        if token in self._known:
            # The first snapshot reports every known node too; setup's
            # membership is the baseline, so only genuinely new keys count.
            return
        self._known.add(token)
        label = change.name or change.node_key
        if change.dev_id not in self._known_devices:
            label = f"{label} ({change.dev_id})"
        self._new_nodes[token] = label
        _LOGGER.debug(
            "New Smartbox node %s/%s detected; a reload is needed to add its "
            "entities",
            change.dev_id,
            change.node_key,
        )
        self._raise_issue()

    def _raise_issue(self) -> None:
        """Create or refresh the reload hint with the missing nodes."""
        labels = sorted(self._new_nodes.values())
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            node_added_issue_id(self._entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_NODE_ADDED_ISSUE,
            translation_placeholders={"nodes": _format_labels(labels)},
        )

    @property
    def new_nodes(self) -> tuple[str, ...]:
        """Display labels for the nodes detected since setup."""
        return tuple(sorted(self._new_nodes.values()))

    @property
    def facts(self) -> dict[str, object]:
        """Membership facts for config-entry diagnostics."""
        issue_id = (DOMAIN, node_added_issue_id(self._entry))
        return {
            "new_nodes": list(self.new_nodes),
            "repair_issue_raised": issue_id in ir.async_get(self._hass).issues,
        }

"""Membership watch: materialize nodes that appear after platform setup.

Entity creation happens once, when the platforms set up from the REST
discovery snapshot. The websocket snapshot is authoritative for membership
and can introduce a node later (a newly paired heater, for example). This
watcher refreshes the REST identity when that happens and asks the
:class:`EntityBuilder` to create the missing devices/nodes in place.

If the refresh or a builder fails, the watcher raises one repair issue per
entry naming the missing nodes and asking for a reload. Removed nodes are
ignored: their entities stay in place and render unavailable, so a backend
glitch cannot delete history or unique ids.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .identity import async_discover_identity
from .smartbox_core.state import NodeAdded

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import SmartboxConfigEntry
    from .entity_builder import EntityBuilder
    from .identity import SmartboxIdentity
    from .smartbox_core.engine import SmartboxEngine
    from .smartbox_core.state import Change
    from .smartbox_protocol.client import SmartboxClient
    from .smartbox_protocol.reseller import SmartboxReseller

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
    """Materialize ``NodeAdded`` membership, falling back to a reload issue."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SmartboxConfigEntry,
        engine: SmartboxEngine,
        identity: SmartboxIdentity,
        builder: EntityBuilder,
        client: SmartboxClient,
        reseller: SmartboxReseller,
    ) -> None:
        """Seed the known sets from discovery and subscribe to the engine."""
        self._hass = hass
        self._entry = entry
        self._builder = builder
        self._client = client
        self._reseller = reseller
        self._known_nodes = {node.node_id for node in identity.nodes}
        self._known_devices = {device.dev_id for device in identity.devices}
        self._pending: dict[str, str] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        engine.subscribe(self._on_change)

    async def _on_change(self, change: Change) -> None:
        """Queue one new node and coalesce a REST refresh."""
        if not isinstance(change, NodeAdded):
            return
        node_id = change.node_id
        if node_id in self._known_nodes or node_id in self._pending:
            # The first snapshot reports every known node too; setup's
            # membership is the baseline, so only genuinely new keys count.
            return
        label = change.name or change.node_key
        if change.dev_id not in self._known_devices:
            label = f"{label} ({change.dev_id})"
        self._pending[node_id] = label
        _LOGGER.debug("New Smartbox node %s detected; refreshing identity", node_id)
        if self._refresh_task is None:
            self._refresh_task = self._hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        """Refresh REST identity and add the missing entities.

        A snapshot can announce several nodes at once; the zero sleep lets the
        remaining ``NodeAdded`` events land before the first network call, so
        one refresh covers the whole addition.
        """
        try:
            await self._builder.wait_ready()
            await asyncio.sleep(0)
            identity = await async_discover_identity(self._client, self._reseller)
            await self._builder.async_add_identity(identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "Automatic entity creation failed; a reload will add %s",
                _format_labels(sorted(self._pending.values())),
            )
            self._raise_issue()
        else:
            found = {node.node_id for node in identity.nodes}
            self._known_nodes.update(found)
            self._known_devices.update(device.dev_id for device in identity.devices)
            for node_id in list(self._pending):
                if node_id in found:
                    self._pending.pop(node_id)
            if self._pending:
                # The snapshot knew a node the REST identity still lacks:
                # keep the hint rather than pretending the entities exist.
                self._raise_issue()
            else:
                clear_node_added_issue(self._hass, self._entry)
        finally:
            self._refresh_task = None

    def _raise_issue(self) -> None:
        """Create or refresh the reload hint with the missing nodes."""
        labels = sorted(self._pending.values())
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
        """Display labels for nodes still awaiting materialization."""
        return tuple(sorted(self._pending.values()))

    @property
    def facts(self) -> dict[str, object]:
        """Membership facts for config-entry diagnostics."""
        issue_id = (DOMAIN, node_added_issue_id(self._entry))
        return {
            "new_nodes": list(self.new_nodes),
            "repair_issue_raised": issue_id in ir.async_get(self._hass).issues,
        }

    @callback
    def close(self) -> None:
        """Cancel a pending refresh when the entry unloads."""
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = None

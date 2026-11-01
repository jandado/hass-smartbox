"""Membership watch: materialize nodes that appear after platform setup.

Entity creation happens once, when the platforms set up from the REST
discovery snapshot. The websocket snapshot is authoritative for membership
and can introduce a node later (a newly paired heater, for example). This
watcher refreshes the REST identity when that happens and asks the
:class:`EntityBuilder` to create the missing devices/nodes in place.

A node that the refreshed identity still lacks is retried with a bounded
backoff, then surfaced as one repair issue per entry asking for a reload.
Removed nodes are ignored: their entities stay in place and render
unavailable, so a backend glitch cannot delete history or unique ids.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .identity import async_discover_identity
from .smartbox_core.state import NodeAdded
from .smartbox_protocol import timing

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import SmartboxConfigEntry
    from .entity_builder import EntityBuilder
    from .history import HistoryImport
    from .identity import SmartboxIdentity
    from .smartbox_core.engine import SmartboxEngine
    from .smartbox_core.state import Change
    from .smartbox_protocol.client import SmartboxClient
    from .smartbox_protocol.reseller import SmartboxReseller

_LOGGER = logging.getLogger(__name__)

_NODE_ADDED_ISSUE = "node_added"
_MAX_LABELS = 10
_MAX_LABEL_LENGTH = 64
_REFRESH_ATTEMPTS = 3
_REFRESH_BACKOFF_SECONDS = (5.0, 15.0, 45.0)

__all__ = ["MembershipWatcher", "clear_node_added_issue", "node_added_issue_id"]


@dataclass(frozen=True)
class _PendingNode:
    """A node announced by membership but not yet materialized."""

    label: str
    dev_id: str
    node_key: str


@callback
def node_added_issue_id(entry: SmartboxConfigEntry) -> str:
    """Per-entry repair issue id for nodes added after setup."""
    return f"{entry.entry_id}_{_NODE_ADDED_ISSUE}"


@callback
def clear_node_added_issue(hass: HomeAssistant, entry: SmartboxConfigEntry) -> None:
    """Clear the reload hint (a successful setup has materialized the nodes)."""
    ir.async_delete_issue(hass, DOMAIN, node_added_issue_id(entry))


def _bounded_label(label: str) -> str:
    """Bound one server-controlled node label for logs, issues and diagnostics."""
    cleaned = "".join(character for character in label if character.isprintable())
    if len(cleaned) <= _MAX_LABEL_LENGTH:
        return cleaned
    return f"{cleaned[: _MAX_LABEL_LENGTH - 1]}…"


def _format_labels(labels: list[str]) -> str:
    """Join node labels, bounding both the count and each label.

    Truncation uses an ellipsis instead of an English "and N more" so the
    placeholder stays inside the translated sentence.
    """
    shown = ", ".join(_bounded_label(label) for label in labels[:_MAX_LABELS])
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
        history: HistoryImport,
    ) -> None:
        """Seed the known sets from discovery and subscribe to the engine."""
        self._hass = hass
        self._entry = entry
        self._engine = engine
        self._builder = builder
        self._client = client
        self._reseller = reseller
        self._history = history
        self._known_nodes = {node.node_id for node in identity.nodes}
        self._known_devices = {device.dev_id for device in identity.devices}
        self._pending: dict[str, _PendingNode] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._closed = False
        self._suspended = False
        engine.subscribe(self._on_change)

    async def _on_change(self, change: Change) -> None:
        """Queue one new node and coalesce a REST refresh."""
        if self._closed or not isinstance(change, NodeAdded):
            return
        node_id = change.node_id
        if node_id in self._known_nodes or node_id in self._pending:
            # The first snapshot reports every known node too; setup's
            # membership is the baseline, so only genuinely new keys count.
            return
        label = change.name or change.node_key
        if change.dev_id not in self._known_devices:
            label = f"{label} ({change.dev_id})"
        self._pending[node_id] = _PendingNode(
            label=_bounded_label(label),
            dev_id=change.dev_id,
            node_key=change.node_key,
        )
        _LOGGER.debug(
            "New Smartbox node %s detected; refreshing identity",
            _bounded_label(node_id),
        )
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Start one refresh task when the watcher is active."""
        if self._closed or self._suspended or self._refresh_task is not None:
            return
        self._refresh_task = self._entry.async_create_task(
            self._hass, self._async_refresh(), "smartbox_membership"
        )

    async def _async_refresh(self) -> None:
        """Refresh identity and add the missing entities, with bounded retries.

        A snapshot can announce several nodes at once; the zero sleep lets the
        remaining ``NodeAdded`` events land before the first network call, so
        one refresh covers the whole addition.
        """
        try:
            await self._builder.wait_ready()
            await timing.sleep(0)
            for attempt in range(_REFRESH_ATTEMPTS):
                if self._closed:
                    return
                try:
                    identity = await self._refresh_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt + 1 >= _REFRESH_ATTEMPTS:
                        _LOGGER.exception(
                            "Automatic entity creation failed after %d attempts",
                            _REFRESH_ATTEMPTS,
                        )
                    else:
                        _LOGGER.warning(
                            "Automatic entity creation failed (attempt %d/%d)",
                            attempt + 1,
                            _REFRESH_ATTEMPTS,
                        )
                else:
                    try:
                        if self._reconcile(identity):
                            return
                    except Exception:
                        _LOGGER.exception("Membership reconciliation failed")
                        self._raise_issue()
                        return
                if attempt + 1 >= _REFRESH_ATTEMPTS:
                    self._raise_issue()
                    return
                await timing.sleep(
                    _REFRESH_BACKOFF_SECONDS[
                        min(attempt, len(_REFRESH_BACKOFF_SECONDS) - 1)
                    ]
                )
        finally:
            self._refresh_task = None

    async def _refresh_once(self) -> SmartboxIdentity:
        """Discover identity and add every missing device/node entity."""
        identity = await async_discover_identity(self._client, self._reseller)
        for node in identity.nodes:
            if node.node_id not in self._known_nodes:
                # Register before the sensor's update_before_add can run.
                self._history.track(node.node_id)
        await self._builder.async_add_identity(identity)
        return identity

    def _reconcile(self, identity: SmartboxIdentity) -> bool:
        """Update the known sets; True when nothing is left pending."""
        before = set(self._known_nodes)
        found = {node.node_id for node in identity.nodes}
        self._known_nodes.update(found)
        self._known_devices.update(device.dev_id for device in identity.devices)
        for node_id in found - before:
            # A node materialized after setup gets the same first-import
            # bookkeeping as a setup-time node.
            self._history.track(node_id)
        for node_id in list(self._pending):
            if node_id in found:
                self._pending.pop(node_id)
        self._drop_vanished_nodes()
        if not self._pending:
            clear_node_added_issue(self._hass, self._entry)
            return True
        return False

    def _drop_vanished_nodes(self) -> None:
        """Forget pending nodes the engine store no longer holds.

        A transient phantom addition can disappear before REST catches up;
        without this the reload hint would live forever.
        """
        for node_id, pending in list(self._pending.items()):
            if self._engine.node(pending.dev_id, pending.node_key) is None:
                self._pending.pop(node_id)

    def _raise_issue(self) -> None:
        """Create or refresh the reload hint with the missing nodes."""
        self._drop_vanished_nodes()
        if not self._pending:
            clear_node_added_issue(self._hass, self._entry)
            return
        labels = sorted(pending.label for pending in self._pending.values())
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
        return tuple(sorted(pending.label for pending in self._pending.values()))

    @property
    def facts(self) -> dict[str, object]:
        """Membership facts for config-entry diagnostics."""
        issue_id = (DOMAIN, node_added_issue_id(self._entry))
        return {
            "new_nodes": list(self.new_nodes),
            "repair_issue_raised": issue_id in ir.async_get(self._hass).issues,
        }

    async def _cancel_refresh(self) -> None:
        """Cancel and await the in-flight refresh task, if any."""
        task = self._refresh_task
        self._refresh_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def async_pause(self) -> None:
        """Stop materializing before the platforms unload."""
        self._suspended = True
        await self._cancel_refresh()

    def resume(self) -> None:
        """Allow materialization again after a refused platform unload."""
        self._suspended = False
        if self._pending:
            self._schedule_refresh()

    @callback
    def close(self) -> None:
        """Stop the watcher when the entry unloads."""
        self._closed = True
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = None

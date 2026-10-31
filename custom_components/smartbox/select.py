"""Support for Smartbox select entities.

Entities read state through the adapter's accessors over ``runtime.engine``
and write through its named ops; updates arrive via the shared
``EngineChangeFanOut``, one registration per node. The REST discovery identity
is kept only for registry identity, device-info fields and the ``priority``
creation gate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory

from .adapter import EngineChangeFanOut, SmartboxNodeAdapterEntity
from .entity_builder import EntityBuilder
from .identity import SmartboxNodeInfo, priority_available
from .smartbox_protocol import VALID_PRIORITIES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import SmartboxConfigEntry, SmartboxRuntime
    from .entity_builder import EntityGroup

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - HA platform signature
    entry: SmartboxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up platform."""
    _LOGGER.debug("Setting up Smartbox select platform")

    runtime = entry.runtime_data
    fanout = runtime.fanout

    def build_node(node: SmartboxNodeInfo) -> list[EntityGroup]:
        """Build the radiator-priority select when the node exposes it."""
        if not priority_available(node):
            return []
        return [([RadiatorPriority(node, entry, runtime, fanout)], True)]

    runtime.entity_builder.register(async_add_entities, build_node=build_node)
    for node in runtime.identity.nodes:
        EntityBuilder.add_groups(build_node(node), async_add_entities)
    _LOGGER.debug("Finished setting up Smartbox select platform")


class SmartboxNodeSelectEntity(SmartboxNodeAdapterEntity, SelectEntity):
    """Base for node-level select entities (identity on the node adapter base)."""

    _attr_entity_category = EntityCategory.CONFIG


class RadiatorPriority(SmartboxNodeSelectEntity):
    """Smartbox radiator priority select (low/medium/high).

    fw-1.9-family setup field only. The options are single-sourced from the
    write layer's ``VALID_PRIORITIES`` (the values observed on the wire and
    offered by the web app). The device takes the priority into account
    itself when enforcing the box power limit; this exposes the setting.
    """

    _attr_icon = "mdi:radiator"

    def __init__(
        self,
        node: SmartboxNodeInfo,
        entry: SmartboxConfigEntry,
        runtime: SmartboxRuntime,
        fanout: EngineChangeFanOut,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(node, entry, runtime, fanout, "priority")
        self._attr_options = list(VALID_PRIORITIES)

    @property
    def current_option(self) -> str | None:
        """Return the current radiator priority."""
        state = self._node_state()
        value = (state.setup if state is not None else {}).get("priority")
        return value if isinstance(value, str) else None

    async def async_select_option(self, option: str) -> None:
        """Set the radiator priority."""
        await self._runtime.engine.set_priority(self._dev_id, self._ref, option)

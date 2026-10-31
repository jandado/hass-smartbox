"""Support for Smartbox binary sensor entities.

Entities read state through the adapter's accessors over ``runtime.engine``;
updates arrive via the shared ``EngineChangeFanOut``. The REST discovery
identity is kept only for registry identity and device-info fields.
"""

import logging
from typing import TYPE_CHECKING

# The component root re-exports this public name without __all__; mypy
# strict's no_implicit_reexport cannot see that re-export.
from homeassistant.components.binary_sensor import (  # type: ignore[attr-defined]
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory

from .adapter import EngineChangeFanOut, SmartboxBoxAdapterEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import SmartboxConfigEntry, SmartboxRuntime
    from .identity import SmartboxDeviceInfo

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - HA platform signature
    entry: SmartboxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up platform."""
    _LOGGER.debug("Setting up Smartbox binary sensor platform")

    runtime = entry.runtime_data
    fanout = runtime.fanout

    # Box connectivity is a device-level fact: one entity on the box device
    # itself.
    async_add_entities(
        [
            Connected(device, entry, runtime, fanout)
            for device in runtime.identity.devices
        ]
    )
    _LOGGER.debug("Finished setting up Smartbox binary sensor platform")


class Connected(SmartboxBoxAdapterEntity, BinarySensorEntity):
    """Smartbox box connectivity sensor."""

    device_class = BinarySensorDeviceClass.CONNECTIVITY
    entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        device: SmartboxDeviceInfo,
        entry: SmartboxConfigEntry,
        runtime: SmartboxRuntime,
        fanout: EngineChangeFanOut,
    ) -> None:
        """Initialize the connectivity sensor."""
        super().__init__(device, entry, runtime, fanout, "connected")

    @property
    def available(self) -> bool:
        """Connectivity is the STATE here, not availability.

        Available whenever the flag is known; a dead feed is the one case where
        it is not, because the flag arrives over that feed.
        """
        if not self._runtime.engine.feed.available:
            return False
        device = self._device_state()
        return device is not None and device.connected is not None

    @property
    def is_on(self) -> bool | None:
        """Return true if the box is connected."""
        device = self._device_state()
        return device.connected if device is not None else None

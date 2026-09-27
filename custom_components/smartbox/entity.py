"""Generic entity."""

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import CONF_DISPLAY_ENTITY_PICTURES, DOMAIN

if TYPE_CHECKING:
    from . import SmartboxConfigEntry
    from .models import SmartboxDevice, SmartboxNode


class DefaultSmartBoxEntity(Entity):
    """Default Smartbox Entity."""

    _node: SmartboxNode
    _attr_key: str
    _attr_websocket_event: str
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: SmartboxConfigEntry) -> None:
        """Initialize the default Device Entity."""
        self._device_id = self._node.node_id
        self._status: dict[str, Any] = {}
        self._attr_translation_key = self._attr_key
        self._attr_unique_id = self._node.node_id
        self._reseller = self._node.session.reseller
        self._configuration_url = f"{self._reseller.web_url}#/{self._node.device.home['id']}/dev/{self._device_id}/{self._node.node_type}/{self._node.addr}/setup"
        if entry.options.get(CONF_DISPLAY_ENTITY_PICTURES, False) is True:
            self._attr_entity_picture = f"{self._reseller.web_url}img/favicon.ico"

    @property
    def unique_id(self) -> str:
        """Return Unique ID string."""
        return f"{self._device_id}_{self._attr_key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._node.name,
            manufacturer=self._reseller.name,
            model_id=str(self._node.device.model_id),
            sw_version=str(self._node.device.sw_version),
            hw_version=str(self._node.hw_version),
            serial_number=str(self._node.device.serial_number),
            configuration_url=self._configuration_url,
        )

    @callback
    def _async_update(self, data: Any) -> None:  # noqa: ANN401
        """Update the state from a websocket event."""
        # Status payloads are dicts snapshotting the node status; keep the
        # entity-local copy in sync for entities reading self._status.
        # Live hardware (../smartbox api-notes.md, 2026-09-26) pushes a
        # transient {"sync_status": "lost"} frame right after every accepted
        # write; skip non-ok frames so the last full snapshot survives until
        # the websocket confirms (same rule as the poll path in async_update).
        if (
            self._attr_websocket_event == "status"
            and data.get("sync_status", "ok") == "ok"
        ):
            self._status = data
        self.async_write_ha_state()


class SmartBoxDeviceEntity(DefaultSmartBoxEntity):
    """BaseClass for SmartBoxDeviceEntity."""

    def __init__(self, device: SmartboxDevice, entry: SmartboxConfigEntry) -> None:
        """Initialize the Device Entity."""
        self._node = next(iter(device.get_nodes()))
        self._device = device
        super().__init__(entry=entry)

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        await super().async_added_to_hass()
        if self._attr_should_poll is False:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{DOMAIN}_{self._device.dev_id}_{self._attr_websocket_event}",
                    self._async_update,
                )
            )


class SmartBoxNodeEntity(DefaultSmartBoxEntity):
    """BaseClass for SmartBoxNodeEntity."""

    def __init__(self, node: SmartboxNode, entry: SmartboxConfigEntry) -> None:
        """Initialize the Node Entity."""
        self._node = node
        super().__init__(entry=entry)

    async def async_update(self) -> None:
        """Get the latest data."""
        new_status = await self._node.async_update(self.hass)
        if new_status["sync_status"] == "ok":
            # update our status
            self._status = new_status
            self._attr_available = True
        else:
            self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        await super().async_added_to_hass()
        if self._attr_should_poll is False:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{DOMAIN}_{self._node.node_id}_{self._attr_websocket_event}",
                    self._async_update,
                )
            )

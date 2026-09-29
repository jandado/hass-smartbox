"""Support for Smartbox switch entities."""

import logging
from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .entity import SmartboxBoxEntity, SmartBoxNodeEntity
from .models import (
    get_boost_end_datetime,
    true_radiant_available,
    window_mode_available,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import SmartboxConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _: HomeAssistant,
    entry: SmartboxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:  # pylint: disable=unused-argument
    """Set up platform."""
    _LOGGER.debug("Setting up Smartbox switch platform")

    switch_entities: list[SwitchEntity] = []
    # Device-level switches live on the box device itself.
    for device in entry.runtime_data.devices:
        _LOGGER.debug("Creating away switch for box %s", device.dev_id)
        switch_entities.append(AwaySwitch(device, entry))
        _LOGGER.debug("Creating no power limit switch for box %s", device.dev_id)
        switch_entities.append(NoPowerLimitSwitch(device, entry))
    for node in entry.runtime_data.nodes:
        if window_mode_available(node):
            _LOGGER.debug("Creating window_mode switch for node %s", node.name)
            switch_entities.append(WindowModeSwitch(node, entry))
        else:
            _LOGGER.info("Window mode not available for node %s", node.name)
        if true_radiant_available(node):
            _LOGGER.debug("Creating true_radiant switch for node %s", node.name)
            switch_entities.append(TrueRadiantSwitch(node, entry))
        else:
            _LOGGER.info("True radiant not available for node %s", node.name)

        if node.boost_available:
            _LOGGER.debug("Creating boost switch for node %s", node.name)
            boost_switch = BoostSwitch(node, entry)
            switch_entities.append(boost_switch)
        else:
            _LOGGER.info("Boost mode not available for node %s", node.name)

    async_add_entities(switch_entities, update_before_add=True)

    _LOGGER.debug("Finished setting up Smartbox switch platform")


class AwaySwitch(SmartboxBoxEntity, SwitchEntity):
    """Smartbox device away switch (box device level)."""

    _attr_key = "away_status"
    _attr_websocket_event = "away_status"

    async def async_turn_on(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn on the switch."""
        await self._device.set_away_status(away=True)

    async def async_turn_off(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn off the switch."""
        await self._device.set_away_status(away=False)

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._device.away

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Return the full away status as attributes."""
        return self._device.away_status


class NoPowerLimitSwitch(SmartboxBoxEntity, SwitchEntity):
    """Smartbox device "no power limit" switch (box device level).

    On the wire the power limit is a single integer where 0 means "no
    limit" (the web app renders it as this toggle plus an editfield for
    the maximum power shared by all radiators). Turning the switch off
    restores the last known non-zero limit; when none is known (typical
    after a restart with no limit active) nothing is written and a
    warning is logged — set a value via the power limit number instead.
    """

    _attr_key = "no_power_limit"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_websocket_event = "power_limit"

    async def async_turn_on(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn on the switch (remove the limit)."""
        await self._device.set_power_limit(0)

    async def async_turn_off(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn off the switch (restore the last known limit)."""
        limit = self._device.last_nonzero_power_limit
        if limit is None:
            _LOGGER.warning(
                "Cannot restore a power limit for device %s: no previous "
                "limit known since startup. Set a value on the power limit "
                "number entity instead.",
                self._device.dev_id,
            )
            return
        await self._device.set_power_limit(limit)

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._device.no_power_limit


class WindowModeSwitch(SmartBoxNodeEntity, SwitchEntity):
    """Smartbox node window mode switch."""

    _attr_key = "window_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_websocket_event = "setup"

    async def async_turn_on(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn on the switch."""
        await self._node.set_window_mode(True)

    async def async_turn_off(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn off the switch."""
        await self._node.set_window_mode(False)

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._node.window_mode


class TrueRadiantSwitch(SmartBoxNodeEntity, SwitchEntity):
    """Smartbox node true radiant switch."""

    _attr_key = "true_radiant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_websocket_event = "setup"

    async def async_turn_on(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn on the switch."""
        await self._node.set_true_radiant(True)

    async def async_turn_off(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn off the switch."""
        await self._node.set_true_radiant(False)

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._node.true_radiant


class BoostSwitch(SmartBoxNodeEntity, SwitchEntity):
    """Smartbox boost switch that activates the device's native boost mode.

    The SmartBox heaters have a built-in boost function that temporarily increases
    the temperature for a configurable amount of time. This switch provides a simple
    toggle to activate/deactivate this functionality.

    The boost temperature and duration can be configured through:
    1. The device's setup through extra_options
    2. The smartbox.set_boost_params service
    """

    _attr_key = "boost"
    _attr_websocket_event = "status"
    _attr_icon = "mdi:rocket-launch"

    @property
    def extra_state_attributes(self) -> dict:
        """Return the state attributes."""
        return {
            "boost_temperature": self._node.boost_temp,
            "boost_duration_minutes": self._node.boost_time,
            "boost_time_remaining": self._node.remaining_boost_time,
            "boost_end_time": get_boost_end_datetime(
                self._node.boost_end_min
            ).strftime("%H:%M")
            if self._node.remaining_boost_time
            else None,
        }

    async def async_turn_on(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn on boost mode."""
        _LOGGER.debug("Activating boost mode for %s", self._node.name)
        await self._node.set_status(boost=True)

    async def async_turn_off(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """Turn off boost mode."""
        _LOGGER.debug("Deactivating boost mode for %s", self._node.name)
        await self._node.set_status(boost=False)

    @property
    def is_on(self) -> bool:
        """Return if boost mode is active."""
        return self._node.boost

"""Binary sensor platform.

The mock world is delivered to the engine as a ws snapshot through the
runtime's FakeTransport (the ``smartbox_stack`` fixture from
test_climate); device-level frames drive connectivity.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.smartbox.const import DOMAIN
from tests.mocks import (
    get_away_status_switch_entity_id,
    get_connected_binary_sensor_entity_id,
    get_device_by_identifier,
    get_entity_id_from_unique_id,
)
from tests.stack import seed_stack


async def test_connected_sensor_updates_without_polling(
    hass, mock_smartbox, config_entry
):
    """The box connectivity entity must react to websocket events, not polls.

    Regression test: the device-level connected event used to be dispatched on
    a device-id signal while the entity listened on a per-node signal, so it
    stayed frozen at its initial value forever. Connectivity now lives on the
    box device itself (one entity, not one per node).
    """
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime, _fake = await seed_stack(hass, mock_smartbox)

    device_dict = (await mock_smartbox.session.get_devices())[0]

    entity_id = get_connected_binary_sensor_entity_id(device_dict)
    assert hass.states.get(entity_id).state == "on"

    # Simulate connectivity loss arriving over the websocket: no poll happens.
    await runtime.transport.deliver_update(
        device_dict["dev_id"], "/connected", {"connected": False}
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    await runtime.transport.deliver_update(
        device_dict["dev_id"], "/connected", {"connected": True}
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"


async def test_connected_false_does_not_take_the_box_controls_offline(
    hass, mock_smartbox, config_entry
):
    """The flag is the box's liveness session, not a statement about its data.

    The backend's session for the box can be down while the box keeps reporting
    through the same feed (docs/api-contract.md §6.4), so box entities follow
    the FEED: the connectivity sensor reports the flag as its STATE, and the
    box controls stay usable instead of being locked for the whole window.
    """
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime, _fake = await seed_stack(hass, mock_smartbox)
    device_dict = (await mock_smartbox.session.get_devices())[0]

    await runtime.transport.deliver_update(
        device_dict["dev_id"], "/connected", {"connected": False}
    )
    await hass.async_block_till_done()

    connectivity = get_connected_binary_sensor_entity_id(device_dict)
    assert hass.states.get(connectivity).state == "off"

    away = get_away_status_switch_entity_id(device_dict)
    away_status = await mock_smartbox.session.get_device_away_status(
        device_dict["dev_id"]
    )
    assert hass.states.get(away).state == ("on" if away_status["away"] else "off")


async def test_connected_sensor_is_on_box_device(hass, mock_smartbox, config_entry):
    """One connectivity entity per box, attached to the box device."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    for device_dict in await mock_smartbox.session.get_devices():
        entity_id = get_connected_binary_sensor_entity_id(device_dict)
        unique_id = f"{device_dict['dev_id']}_connected"
        assert get_entity_id_from_unique_id(
            hass, BINARY_SENSOR_DOMAIN, unique_id
        ) == entity_id
        box_device = get_device_by_identifier(
            device_registry, (DOMAIN, device_dict["dev_id"])
        )
        assert box_device is not None
        entity_entry = entity_registry.async_get(entity_id)
        assert entity_entry.device_id == box_device.id

"""Tests for the Smartbox binary sensor platform."""

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN

from custom_components.smartbox.const import DOMAIN

from .mocks import get_entity_id_from_unique_id, get_node_unique_id


async def test_connected_sensor_updates_without_polling(
    hass, mock_smartbox, config_entry
):
    """Connected entities must react to websocket events, not polls.

    Regression test: the device-level connected event used to be dispatched on
    a device-id signal while these entities listened on per-node signals, so
    they stayed frozen at their initial value forever.
    """
    mock_smartbox.session.get_device_connected.return_value = {"connected": True}
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    device_dict = (await mock_smartbox.session.get_devices())[0]
    runtime_device = hass.config_entries.async_entries(DOMAIN)[
        0
    ].runtime_data.devices[0]
    assert runtime_device.dev_id == device_dict["dev_id"]

    entity_ids = []
    for node in await mock_smartbox.session.get_nodes(device_dict["dev_id"]):
        entity_id = get_entity_id_from_unique_id(
            hass,
            BINARY_SENSOR_DOMAIN,
            get_node_unique_id(device_dict, node, "connected"),
        )
        entity_ids.append(entity_id)
        assert hass.states.get(entity_id).state == "on"

    # Simulate connectivity loss arriving over the websocket: no poll happens.
    runtime_device._connected(connected=False)
    await hass.async_block_till_done()
    for entity_id in entity_ids:
        assert hass.states.get(entity_id).state == "off"

    runtime_device._connected(connected=True)
    await hass.async_block_till_done()
    for entity_id in entity_ids:
        assert hass.states.get(entity_id).state == "on"


async def test_lock_binary_sensor(hass, mock_smartbox, config_entry):
    """Lock sensors exist for heater nodes and follow status websocket events."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    device_dict = (await mock_smartbox.session.get_devices())[0]
    node = (await mock_smartbox.session.get_nodes(device_dict["dev_id"]))[0]
    entity_id = get_entity_id_from_unique_id(
        hass, BINARY_SENSOR_DOMAIN, get_node_unique_id(device_dict, node, "lock")
    )
    # Mock heater node starts unlocked (locked == 0).
    assert hass.states.get(entity_id).state == "on"

    mock_smartbox.generate_socket_status_update(device_dict, node, {"locked": 1})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

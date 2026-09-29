from homeassistant.components.number import ATTR_VALUE, DOMAIN as NUMBER_DOMAIN
from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, ATTR_FRIENDLY_NAME
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.smartbox.const import DOMAIN, SERVICE_SET_BOOST_PARAMS

from .const import MOCK_SMARTBOX_DEVICE_POWER
from .mocks import (
    get_boost_duration_entity_id,
    get_boost_duration_entity_name,
    get_boost_temperature_entity_id,
    get_boost_temperature_entity_name,
    get_device_unique_id,
    get_entity_id_from_unique_id,
    get_node_unique_id,
    get_object_id,
    get_power_limit_number_entity_id,
    get_power_limit_number_entity_name,
)
from .test_utils import convert_temp, round_temp


async def test_power_limit(hass, mock_smartbox, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_entity_ids(NUMBER_DOMAIN)) == 12
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1

    assert DOMAIN in hass.config.components
    # One power limit entity per box device, always created (0 = no limit).
    for mock_device in await mock_smartbox.session.get_devices():
        entity_id = get_entity_id_from_unique_id(
            hass, NUMBER_DOMAIN, get_device_unique_id(mock_device, "power_limit")
        )
        state = hass.states.get(entity_id)
        # check basic properties
        assert state.object_id.startswith(
            get_object_id(get_power_limit_number_entity_name(mock_device))
        )
        assert state.name == f"{mock_device['name']} Power Limit"
        assert (
            state.attributes[ATTR_FRIENDLY_NAME]
            == f"{mock_device['name']} Power Limit"
        )
        assert entity_id == get_power_limit_number_entity_id(mock_device)
        assert state.attributes["max"] == 60000
        assert state.attributes["min"] == 0
        assert state.state == str(MOCK_SMARTBOX_DEVICE_POWER[mock_device["dev_id"]])

    mock_device_1 = (await mock_smartbox.session.get_devices())[0]
    runtime_device = hass.config_entries.async_entries(DOMAIN)[0].runtime_data.devices[
        0
    ]
    assert runtime_device.dev_id == mock_device_1["dev_id"]

    entity_id = get_power_limit_number_entity_id(mock_device_1)
    await async_update_entity(hass, entity_id)
    state = hass.states.get(entity_id)
    assert state.state == "1500"

    entity_id = get_power_limit_number_entity_id(mock_device_1)
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 500},
        blocking=True,
    )
    state = hass.states.get(entity_id)
    assert state.state == "500"
    assert runtime_device.power_limit == 500


async def test_boost_temperature(hass, mock_smartbox, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_entity_ids(NUMBER_DOMAIN)) == 12
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1

    assert DOMAIN in hass.config.components
    mock_device = (await mock_smartbox.session.get_devices())[1]
    mock_node = (await mock_smartbox.session.get_nodes(mock_device["dev_id"]))[3]
    entity_id = get_boost_temperature_entity_id(mock_node)
    state = hass.states.get(entity_id)
    # check basic properties
    assert state.object_id.startswith(
        get_object_id(get_boost_temperature_entity_name(mock_node))
    )
    assert state.entity_id.startswith(get_boost_temperature_entity_id(mock_node))
    assert state.name == f"{mock_node['name']} Boost temperature"
    assert (
        state.attributes[ATTR_FRIENDLY_NAME] == f"{mock_node['name']} Boost temperature"
    )
    unique_id = get_node_unique_id(mock_device, mock_node, "config_boost_temperature")
    assert entity_id == get_entity_id_from_unique_id(hass, NUMBER_DOMAIN, unique_id)
    min_val = state.attributes.get("min")
    max_val = state.attributes.get("max")
    test_value = round((min_val + max_val) / 2, 1)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: test_value},
        blocking=True,
    )
    await async_update_entity(hass, entity_id)
    state = hass.states.get(entity_id)
    assert state.state == str(test_value)


async def test_boost_duration(hass, mock_smartbox, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_entity_ids(NUMBER_DOMAIN)) == 12
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1

    assert DOMAIN in hass.config.components
    mock_device = (await mock_smartbox.session.get_devices())[1]
    mock_node = (await mock_smartbox.session.get_nodes(mock_device["dev_id"]))[3]
    entity_id = get_boost_duration_entity_id(mock_node)
    state = hass.states.get(entity_id)
    # check basic properties
    assert state.object_id.startswith(
        get_object_id(get_boost_duration_entity_name(mock_node))
    )
    assert state.entity_id.startswith(get_boost_duration_entity_id(mock_node))
    assert state.name == f"{mock_node['name']} Boost duration"
    assert state.attributes[ATTR_FRIENDLY_NAME] == f"{mock_node['name']} Boost duration"
    unique_id = get_node_unique_id(mock_device, mock_node, "config_boost_duration")
    assert entity_id == get_entity_id_from_unique_id(hass, NUMBER_DOMAIN, unique_id)
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 120.0},
        blocking=True,
    )
    # Faut simuler le retour de la websocket
    await async_update_entity(hass, entity_id)
    state = hass.states.get(entity_id)
    assert state.state == "120.0"


async def test_set_boost_params_service_targets_entities(
    hass, mock_smartbox, config_entry
):
    """Test the smartbox.set_boost_params service with entity_id targets."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_device = (await mock_smartbox.session.get_devices())[1]
    mock_node = (await mock_smartbox.session.get_nodes(mock_device["dev_id"]))[3]
    temp_entity_id = get_boost_temperature_entity_id(mock_node)
    duration_entity_id = get_boost_duration_entity_id(mock_node)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_BOOST_PARAMS,
        {
            ATTR_ENTITY_ID: [temp_entity_id, duration_entity_id],
            "temperature": 21.5,
            "duration": 120,
        },
        blocking=True,
    )

    # Simulate the websocket state refresh
    await async_update_entity(hass, temp_entity_id)
    await async_update_entity(hass, duration_entity_id)
    mock_node_status = await mock_smartbox.session.get_status(
        mock_device["dev_id"], mock_node
    )
    state = hass.states.get(temp_entity_id)
    expected_temp = round_temp(
        hass, convert_temp(hass, mock_node_status["units"], 21.5)
    )
    assert state.state == str(expected_temp)
    state = hass.states.get(duration_entity_id)
    assert state.state == "120.0"

    # The service must send merged extra_options, preserving unrelated keys
    setup = await mock_smartbox.session.get_node_setup(mock_device["dev_id"], mock_node)
    assert setup["extra_options"]["boost_temp"] == "21.5"
    assert setup["extra_options"]["boost_time"] == 120

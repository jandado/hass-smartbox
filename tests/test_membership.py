"""Tests for the post-setup node membership watch."""

from homeassistant.helpers import issue_registry as ir

from custom_components.smartbox.const import DOMAIN, SmartboxNodeType
from custom_components.smartbox.diagnostics import async_get_config_entry_diagnostics
from custom_components.smartbox.membership import node_added_issue_id


def _snapshot_with_new_node() -> list[dict]:
    """Build a device_1 snapshot carrying its known nodes plus one new htr."""
    return [
        {
            "id": "home_1",
            "devs": [
                {
                    "dev_id": "device_1",
                    "devData": {
                        "connected": True,
                        "away_status": {"away": False},
                        "nodes": [
                            {
                                "type": "htr",
                                "addr": 0,
                                "status": {"stemp": "20.0", "units": "C"},
                                "setup": {"priority": "low"},
                                "version": {"pid": "081c", "hw_version": "2.3"},
                            },
                            {
                                "type": "acm",
                                "addr": 1,
                                "status": {"stemp": "20.0", "units": "C"},
                                "setup": {"priority": "low"},
                                "version": {"pid": "081c", "hw_version": "2.3"},
                            },
                            {
                                "type": "htr",
                                "addr": 2,
                                "name": "Desk Heater",
                                "status": {"stemp": "19.0", "units": "C"},
                                "setup": {"priority": "low"},
                                "version": {"pid": "081c", "hw_version": "2.3"},
                            },
                        ],
                    },
                }
            ],
        }
    ]


async def test_new_node_raises_one_reload_issue_and_reports_it(
    hass, mock_smartbox, config_entry
):
    """A snapshot node missing from REST discovery prompts a reload."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await config_entry.runtime_data.transport.deliver_snapshot(
        _snapshot_with_new_node()
    )
    await hass.async_block_till_done()

    issue_id = (DOMAIN, node_added_issue_id(config_entry))
    issue = ir.async_get(hass).issues[issue_id]
    assert issue.translation_key == "node_added"
    assert issue.translation_placeholders == {"nodes": "Desk Heater"}

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    membership = result["runtime"]["membership"]
    assert membership == {
        "new_nodes": ["Desk Heater"],
        "repair_issue_raised": True,
    }

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue_id not in ir.async_get(hass).issues

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    assert result["runtime"]["membership"] == {
        "new_nodes": [],
        "repair_issue_raised": False,
    }


async def test_new_node_is_materialized_without_reload(
    hass, mock_smartbox, config_entry
):
    """A node present in REST discovery is added in place, with no repair issue."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data

    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_9",
            "fw_version": "fw_version_1_9",
            "serial_id": "serial_id_1_9",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )

    await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
    await hass.async_block_till_done()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues
    assert hass.states.get("climate.desk_heater") is not None
    assert hass.states.get("sensor.desk_heater_power") is not None
    assert any(
        entity.entity_id == "climate.desk_heater" for entity in runtime.services.climate
    )
    assert runtime.membership.facts == {
        "new_nodes": [],
        "repair_issue_raised": False,
    }

    # A repeated snapshot must not build the same node twice.
    await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
    await hass.async_block_till_done()
    climate_entities = [
        entity
        for entity in runtime.services.climate
        if entity.entity_id == "climate.desk_heater"
    ]
    assert len(climate_entities) == 1


async def test_new_device_is_materialized_without_reload(
    hass, mock_smartbox, config_entry
):
    """A newly paired box gets its box and node entities in place."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_smartbox.add_device(
        {
            "dev_id": "device_3",
            "name": "Device 3",
            "product_id": "product_id_3",
            "fw_version": "fw_version_3",
            "serial_id": "serial_id_3",
        },
        {
            "addr": 0,
            "name": "Device 3 0",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_3_0",
            "fw_version": "fw_version_3_0",
            "serial_id": "serial_id_3_0",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )

    await config_entry.runtime_data.transport.deliver_snapshot(
        [
            {
                "id": "home_1",
                "devs": [
                    {
                        "dev_id": "device_3",
                        "devData": {
                            "connected": True,
                            "away_status": {"away": False},
                            "nodes": [
                                {
                                    "type": "htr",
                                    "addr": 0,
                                    "status": {"stemp": "20.0", "units": "C"},
                                    "setup": {"priority": "low"},
                                    "version": {"pid": "081c", "hw_version": "2.3"},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    )
    await hass.async_block_till_done()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues
    assert hass.states.get("binary_sensor.device_3_connectivity") is not None
    assert hass.states.get("climate.device_3_0") is not None
    assert hass.states.get("number.device_3_power_limit") is not None


async def test_a_known_node_does_not_raise(hass, mock_smartbox, config_entry):
    """The first snapshot reports the setup-time nodes; none is 'new'."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await config_entry.runtime_data.transport.deliver_snapshot(
        [
            {
                "id": "home_1",
                "devs": [
                    {
                        "dev_id": "device_1",
                        "devData": {
                            "connected": True,
                            "away_status": {"away": False},
                            "nodes": [
                                {
                                    "type": "htr",
                                    "addr": 0,
                                    "status": {"stemp": "20.0", "units": "C"},
                                    "setup": {"priority": "low"},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    )
    await hass.async_block_till_done()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues

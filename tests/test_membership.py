"""Tests for the post-setup node membership watch."""

from homeassistant.helpers import issue_registry as ir

from custom_components.smartbox.const import DOMAIN
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
                                "addr": 9,
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
    """A new node prompts a reload; known nodes stay quiet."""
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

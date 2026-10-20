"""Tests for the integration's system health page."""

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.smartbox.smartbox_protocol.client import SmartboxClient
from custom_components.smartbox.smartbox_protocol.errors import SmartboxError
from custom_components.smartbox.system_health import async_register, system_health_info


async def test_system_health_no_entries(hass):
    """Without a config entry there is nothing to report."""
    assert await system_health_info(hass) == {}


async def test_system_health_api_facts(hass, mock_smartbox, config_entry):
    """API version/health read over the entry's client; reseller URL checked."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with (
        patch.object(
            SmartboxClient,
            "get_api_version",
            new=AsyncMock(
                return_value={"major": 1, "minor": 2, "subminor": 3, "commit": "abc"}
            ),
        ),
        patch.object(
            SmartboxClient,
            "health_check",
            new=AsyncMock(return_value={"message": "All good"}),
        ),
        patch(
            "homeassistant.components.system_health.async_check_can_reach_url",
            new=AsyncMock(return_value="reachable"),
        ) as reach,
    ):
        info = await system_health_info(hass)

    assert info["api_version"] == "1.2.3.abc"
    assert info["api_health_check"] == "All good"
    # System health awaits the reachability coroutine itself.
    assert await info["can_reach_web_reseller"] == "reachable"
    runtime = config_entry.runtime_data
    assert reach.await_args.args[1] == runtime.identity.devices[0].reseller.web_url


async def test_system_health_maps_malformed_reads(hass, mock_smartbox, config_entry):
    """A malformed success is reported as an error, not raised."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with (
        patch.object(
            SmartboxClient,
            "get_api_version",
            new=AsyncMock(side_effect=SmartboxError("bad")),
        ),
        patch.object(
            SmartboxClient,
            "health_check",
            new=AsyncMock(side_effect=KeyError("message")),
        ),
        patch(
            "homeassistant.components.system_health.async_check_can_reach_url",
            new=AsyncMock(return_value="reachable"),
        ),
    ):
        info = await system_health_info(hass)

    assert info["api_version"] == "error: SmartboxError"
    assert info["api_health_check"] == "error: KeyError"
    assert await info["can_reach_web_reseller"] == "reachable"


async def test_system_health_registers_its_callback(hass):
    """The system-health registration points at the info callback."""
    registration = MagicMock()
    async_register(hass, registration)
    registration.async_register_info.assert_called_once_with(system_health_info)


async def test_system_health_failing_entry(hass, mock_smartbox, config_entry):
    """A failing/retrying entry reports unknown facts instead of crashing."""
    info = await system_health_info(hass)
    assert info == {
        "api_version": None,
        "api_health_check": None,
        "can_reach_web_reseller": None,
    }

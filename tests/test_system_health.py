"""Tests for the integration's system health page."""

import asyncio
from unittest.mock import AsyncMock, patch

from custom_components.smartbox.smartbox_protocol.client import SmartboxClient
from custom_components.smartbox.system_health import system_health_info


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
    ):
        info = await system_health_info(hass)

    assert info["api_version"] == "1.2.3.abc"
    assert info["api_health_check"] == "All good"
    # The reseller reachability check is a coroutine (system health awaits it).
    assert asyncio.iscoroutine(info["can_reach_web_reseller"])
    info["can_reach_web_reseller"].close()  # avoid "never awaited" noise


async def test_system_health_failing_entry(hass, mock_smartbox, config_entry):
    """A failing/retrying entry reports unknown facts instead of crashing."""
    info = await system_health_info(hass)
    assert info == {
        "api_version": None,
        "api_health_check": None,
        "can_reach_web_reseller": None,
    }

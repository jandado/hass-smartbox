import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import pytest

from custom_components.smartbox import (
    APIUnavailableError,
    InvalidAuthError,
    SmartboxError,
    async_setup_entry,
    create_smartbox_session_from_entry,
    update_listener,
)


@pytest.mark.asyncio
async def test_async_setup_entry_auth_failed(hass, config_entry):
    with (
        patch(
            "custom_components.smartbox.create_smartbox_session_from_entry",
            side_effect=InvalidAuthError,
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, config_entry)
    with (
        patch(
            "custom_components.smartbox.create_smartbox_session_from_entry",
            side_effect=SmartboxError,
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, config_entry)


@pytest.mark.asyncio
async def test_create_smartbox_session_from_entry_success(
    hass, config_entry, mock_session
):
    with (
        patch(
            "custom_components.smartbox.async_get_clientsession",
            return_value=AsyncMock(),
        ),
        patch(
            "custom_components.smartbox.AsyncSmartboxSession",
            return_value=mock_session,
        ),
    ):
        session = await create_smartbox_session_from_entry(hass, config_entry)
        assert session is not None
        assert session.health_check.called
        assert session.check_refresh_auth.called


@pytest.mark.asyncio
async def test_create_smartbox_session_from_entry_api_unavailable(hass, config_entry):
    with (
        patch(
            "custom_components.smartbox.async_get_clientsession",
            return_value=AsyncMock(),
        ),
        patch(
            "custom_components.smartbox.AsyncSmartboxSession",
            side_effect=APIUnavailableError,
        ),
        pytest.raises(APIUnavailableError),
    ):
        await create_smartbox_session_from_entry(hass, config_entry)


@pytest.mark.asyncio
async def test_create_smartbox_session_from_entry_invalid_auth(hass, config_entry):
    with (
        patch(
            "custom_components.smartbox.async_get_clientsession",
            return_value=AsyncMock(),
        ),
        patch(
            "custom_components.smartbox.AsyncSmartboxSession",
            side_effect=InvalidAuthError,
        ),
        pytest.raises(InvalidAuthError),
    ):
        await create_smartbox_session_from_entry(hass, config_entry)


@pytest.mark.asyncio
async def test_create_smartbox_session_from_entry_smartbox_error(hass, config_entry):
    with (
        patch(
            "custom_components.smartbox.async_get_clientsession",
            return_value=AsyncMock(),
        ),
        patch(
            "custom_components.smartbox.AsyncSmartboxSession",
            side_effect=SmartboxError,
        ),
        pytest.raises(SmartboxError),
    ):
        await create_smartbox_session_from_entry(hass, config_entry)


@pytest.mark.asyncio
async def test_update_listener(hass, config_entry):
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await update_listener(hass, config_entry)
        mock_reload.assert_called_once_with(config_entry.entry_id)


async def test_device_cancel_bounded_when_socket_teardown_hangs(
    hass, mock_smartbox, config_entry
):
    """Regression: wedged socket teardown must neither hang nor raise.

    On live hardware run() parks in the websocket loop, so at cancel time the
    watchdog task is still alive. cancel() used to await the cancelled
    watchdog task bare, raising CancelledError out of async_unload_entry
    (UI reload died silently), and awaited update_manager.cancel() unbounded.
    """
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    device = config_entry.runtime_data.devices[0]

    never = asyncio.Event()
    hang_cancel = AsyncMock(side_effect=never.wait)
    # The mock socket's run() parks forever (like live hardware), so the
    # watchdog task created by setup is already the parked task we need.

    with (
        patch.object(device.update_manager, "cancel", hang_cancel),
        patch("custom_components.smartbox.models._TEARDOWN_TIMEOUT_SECONDS", 0.05),
    ):
        cancel_task = asyncio.create_task(device.cancel())
        done, _pending = await asyncio.wait({cancel_task}, timeout=5)
        assert done, "device.cancel() hung (regression)"
        # Must complete without raising (CancelledError used to escape).
        await cancel_task

    assert device._watchdog_task.cancelled()
    assert hang_cancel.await_count == 1


async def test_unload_entry_completes_with_parked_watchdog(
    hass, mock_smartbox, config_entry
):
    """Regression: UI reload unloads the entry first; unload must finish."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    devices = list(config_entry.runtime_data.devices)
    # The mock socket's run() parks forever (like live hardware), so the
    # setup-created watchdog task is already parked as this test needs.
    never = asyncio.Event()
    for device in devices:
        device.update_manager.cancel = AsyncMock(side_effect=never.wait)

    with patch("custom_components.smartbox.models._TEARDOWN_TIMEOUT_SECONDS", 0.05):
        unload_task = asyncio.create_task(
            hass.config_entries.async_unload(config_entry.entry_id)
        )
        done, _pending = await asyncio.wait({unload_task}, timeout=5)
        assert done, "async_unload_entry hung (regression)"
        assert unload_task.result() is True

    for device in devices:
        assert device._watchdog_task.cancelled()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_hass_stop_cancels_device_websocket_tasks(
    hass, mock_smartbox, config_entry
):
    """Regression: HA shutdown used to leave the watchdog tasks running."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    devices = config_entry.runtime_data.devices

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    for device in devices:
        assert device._watchdog_task.cancelled()

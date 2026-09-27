"""The Smartbox integration."""

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from smartbox import AsyncSmartboxSession
from smartbox.error import APIUnavailableError, InvalidAuthError, SmartboxError

from .const import CONF_API_NAME, DOMAIN
from .models import SmartboxDevice, SmartboxNode, get_devices

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

__version__ = "2.4.0"

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

type SmartboxConfigEntry = ConfigEntry[SmartboxData]


@dataclass
class SmartboxData:
    """Runtime data for the Smartbox class."""

    client: AsyncSmartboxSession
    devices: list[SmartboxDevice]
    nodes: list[SmartboxNode]


async def create_smartbox_session_from_entry(
    hass: HomeAssistant,
    entry: SmartboxConfigEntry | dict[str, Any],
) -> AsyncSmartboxSession:
    """Create a Session class from smartbox."""
    if isinstance(entry, dict):
        data: dict[str, Any] = entry
    else:
        data = dict(entry.data)
    websession = async_get_clientsession(hass)
    session = AsyncSmartboxSession(
        api_name=data[CONF_API_NAME],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        websession=websession,
    )
    await session.health_check()
    await session.check_refresh_auth()
    return session


def _async_wire_reauth(
    hass: HomeAssistant,
    entry: SmartboxConfigEntry,
) -> None:
    """Wire per-device rejected-credentials signals to the reauth flow.

    A websocket loop dying with rejected credentials surfaces here so the
    entry can start its reauthentication flow (models.py stays entry-free).
    """
    for device in entry.runtime_data.devices:

        @callback
        def _async_reauth_required(_device: SmartboxDevice = device) -> None:
            _LOGGER.warning(
                "Smartbox credentials rejected for device %s; "
                "starting reauthentication flow",
                _device.dev_id,
            )
            entry.async_start_reauth(hass)

        entry.async_on_unload(
            async_dispatcher_connect(
                hass,
                f"{DOMAIN}_{device.dev_id}_reauth_required",
                _async_reauth_required,
            )
        )


async def async_setup_entry(hass: HomeAssistant, entry: SmartboxConfigEntry) -> bool:
    """Set up Smartbox from a config entry."""
    try:
        entry.runtime_data = SmartboxData(
            client=(await create_smartbox_session_from_entry(hass, entry)),
            devices=[],
            nodes=[],
        )
    except InvalidAuthError as ex:
        raise ConfigEntryAuthFailed from ex
    except (SmartboxError, APIUnavailableError) as ex:
        raise ConfigEntryNotReady from ex

    try:
        devices = await get_devices(session=entry.runtime_data.client, hass=hass)
    except InvalidAuthError as ex:
        raise ConfigEntryAuthFailed from ex
    except (SmartboxError, APIUnavailableError) as ex:
        raise ConfigEntryNotReady from ex
    for device in devices:
        _LOGGER.info("Setting up configured device %s", device.dev_id)
        entry.runtime_data.devices.append(device)
    for device in entry.runtime_data.devices:
        nodes = device.get_nodes()
        _LOGGER.debug("Configuring nodes for device %s %s", device.dev_id, nodes)
        entry.runtime_data.nodes.extend(nodes)

    # A websocket loop dying with rejected credentials surfaces here so the
    # entry can start its reauthentication flow (models.py stays entry-free).
    _async_wire_reauth(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _async_cancel_devices_on_hass_stop(_event: Event) -> None:
        """Stop the device websocket sessions when Home Assistant stops."""
        for device in entry.runtime_data.devices:
            hass.async_create_task(device.cancel())

    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _async_cancel_devices_on_hass_stop
        )
    )
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartboxConfigEntry) -> bool:
    """Unload a config entry."""
    # runtime_data only exists once setup got far enough to build the session;
    # a failed/retrying entry must still unload cleanly (the UI reload path).
    runtime_data: SmartboxData | None = getattr(entry, "runtime_data", None)
    if runtime_data is not None:
        for device in runtime_data.devices:
            await device.cancel()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def update_listener(hass: HomeAssistant, entry: SmartboxConfigEntry) -> None:
    """Reload entity from config entry."""
    await hass.config_entries.async_reload(entry.entry_id)

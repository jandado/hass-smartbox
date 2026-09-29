"""The Smartbox integration."""

from dataclasses import dataclass
import logging
import re
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
from homeassistant.helpers import device_registry as dr, entity_registry as er
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
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Device-level entity keys that existed on per-node devices before the box
# device was introduced; their registry entries are re-keyed on setup.
_REHOMED_KEYS = ("away_status", "connected", "power_limit")

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


def _async_home_box_devices(
    hass: HomeAssistant, entry: SmartboxConfigEntry
) -> dict[str, str]:
    """Create the box device for each configured Smartbox device.

    Returns dev_id -> device registry id. The device's display fields are
    written both here and by the box entities' device_info.
    """
    registry = dr.async_get(hass)
    return {
        device.dev_id: registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device.dev_id)},
            manufacturer=device.session.reseller.name,
            name=device.name,
            model_id=str(device.model_id),
            sw_version=str(device.sw_version),
            serial_number=str(device.serial_number),
            configuration_url=(
                f"{device.session.reseller.web_url}#{device.home['id']}"
            ),
        ).id
        for device in entry.runtime_data.devices
    }


def _async_rehome_device_entities(
    hass: HomeAssistant, entry: SmartboxConfigEntry, box_device_ids: dict[str, str]
) -> None:
    """Move pre-existing device-level registry entries onto the box device.

    Before the box device existed, the away switch, connectivity sensor
    and power limit were created per node with unique_ids
    ``{dev_id}_{addr}_{key}``. Re-key the lowest-addr entry per key to
    ``{dev_id}_{key}`` on the box device (keeping its history and
    settings) and remove the duplicates beyond it. Idempotent: entries
    already on a box device no longer match the per-node pattern.
    """
    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    for dev_id, box_device_id in box_device_ids.items():
        for key in _REHOMED_KEYS:
            pattern = re.compile(rf"^{re.escape(dev_id)}_(\d+)_{key}$")
            matches = []
            for registry_entry in entries:
                if registry_entry.platform != DOMAIN:
                    continue
                if match := pattern.fullmatch(registry_entry.unique_id):
                    matches.append((int(match.group(1)), registry_entry))
            if not matches:
                continue
            matches.sort(key=lambda item: item[0])
            _, first = matches[0]
            new_unique_id = f"{dev_id}_{key}"
            if (
                entity_registry.async_get_entity_id(first.domain, DOMAIN, new_unique_id)
                is None
            ):
                entity_registry.async_update_entity(
                    first.entity_id,
                    new_unique_id=new_unique_id,
                    device_id=box_device_id,
                )
            else:
                # Target already re-keyed by a partial earlier run: this
                # old per-node entry is itself a leftover duplicate.
                entity_registry.async_remove(first.entity_id)
            for _, duplicate in matches[1:]:
                entity_registry.async_remove(duplicate.entity_id)


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

    # The box itself is a Home Assistant device; create it (and re-home the
    # device-level registry entries from the per-node devices) before the
    # platforms attach their entities to it.
    box_device_ids = _async_home_box_devices(hass, entry)
    _async_rehome_device_entities(hass, entry, box_device_ids)

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

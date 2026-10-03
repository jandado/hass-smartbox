"""Diagnostics for Smartbox Integration."""

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr, entity_registry as er

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import SmartboxConfigEntry

TO_REDACT = [CONF_PASSWORD, CONF_USERNAME, "title", "unique_id"]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: SmartboxConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = config_entry.runtime_data
    diagnostics_data: dict[str, Any] = {
        "entry": async_redact_data(config_entry.as_dict(), TO_REDACT),
        "runtime_data": {
            # The active transport is the first thing debugging stale
            # entities needs (a ws_user socket restarting reads very
            # differently from dead per-device sockets).
            "transport": "ws_user" if runtime.ws_user_socket else "socket_io",
            # The library's `closed` means "the run loop finished" (the
            # supervision loop is dead), NOT per-connection state —
            # named accordingly to keep bug reports readable.
            "ws_user_run_finished": (
                None if runtime.ws_user_socket is None else runtime.ws_user_socket.closed
            ),
            "client": {"expiry_time": runtime.client.expiry_time},
            "nodes": [
                {"info": e.node_info, "setup": e.setup, "status": e.status}
                for e in config_entry.runtime_data.nodes
            ],
            "devices": [d.device for d in config_entry.runtime_data.devices],
        },
    }
    diagnostics_data["hass_devices"] = [
        e.dict_repr
        for e in dr.async_entries_for_config_entry(
            dr.async_get(hass), config_entry.entry_id
        )
    ]
    diagnostics_data["hass_entities"] = [
        e.as_partial_dict
        for e in er.async_entries_for_config_entry(
            er.async_get(hass), config_entry.entry_id
        )
    ]
    return diagnostics_data

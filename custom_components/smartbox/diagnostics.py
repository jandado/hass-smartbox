"""Diagnostics for Smartbox Integration.

Everything debuggable comes from the runtime: the supervised ws_user
transport, the engine's terminal supervision state, the token expiry, the
engine store, and the deadlines that will flip a node Unavailable. Credential
material never reaches the store, so the entry dict is the only redacted part.
"""

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from . import ws_user_flapping_issue_id
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import SmartboxConfigEntry, SmartboxRuntime

TO_REDACT = [CONF_PASSWORD, CONF_USERNAME, "title", "unique_id"]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: SmartboxConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # getattr: the attribute is ABSENT while setup never ran/retried.
    runtime: SmartboxRuntime | None = getattr(config_entry, "runtime_data", None)
    diagnostics_data: dict[str, Any] = {
        "entry": async_redact_data(config_entry.as_dict(), TO_REDACT),
        # A runtime is absent while the entry is failing/retrying setup.
        "runtime": _runtime_diagnostics(runtime),
        "ws_user_recovery": _ws_user_recovery_diagnostics(hass, config_entry),
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


def _runtime_diagnostics(runtime: SmartboxRuntime | None) -> dict[str, Any]:
    """Transport, supervision, token and engine-store facts from the runtime.

    The transport comes first: a supervised ws_user transport restarting reads
    very differently from a dead socket.
    """
    if runtime is None:
        return {
            "transport": None,
            "supervision": None,
            "token_expiry": None,
            "feed": None,
            "membership": None,
            "statistics_import": None,
        }
    return {
        "transport": {"type": "ws_user", **runtime.transport.facts},
        # Terminal supervision state (ws_user disappeared / rejected
        # credentials): the monitor re-probes or starts reauth on these, so a
        # stuck entry shows up here.
        "supervision": {
            "unsupported": runtime.engine.unsupported,
            "auth_failed": runtime.engine.auth_failed,
            "terminal_error": (
                None
                if runtime.engine.terminal_error is None
                else str(runtime.engine.terminal_error)
            ),
        },
        "token_expiry": (
            None
            if (expiry := runtime.auth.token_expiry) is None
            else expiry.isoformat()
        ),
        # Feed (websocket) state: whether values are still flowing, why the
        # feed went down, how long it has been down and the outage history.
        "feed": runtime.engine.feed.facts,
        # Nodes reported after setup: the reload hint and what it lists.
        "membership": runtime.membership.facts,
        # First-setup history import: which nodes are still pending, which
        # completed, and the backoff/error state of the ones that failed.
        "statistics_import": runtime.history.diagnostics(),
        "engine": _engine_diagnostics(runtime),
    }


def _ws_user_recovery_diagnostics(
    hass: HomeAssistant, config_entry: SmartboxConfigEntry
) -> dict[str, Any]:
    """Report whether the ws_user repair issue is raised.

    The in-place re-probe streak lives on the monitor task, so the repair issue
    is the durable signal that the endpoint has been refusing.
    """
    issue_id = (DOMAIN, ws_user_flapping_issue_id(config_entry))
    return {"repair_issue_raised": issue_id in ir.async_get(hass).issues}


def _engine_diagnostics(runtime: SmartboxRuntime) -> dict[str, Any]:
    """Return the engine store: device slots + per-node slots (raw wire)."""
    devices: dict[str, Any] = {}
    for dev_id, device in runtime.engine.world.devices.items():
        devices[dev_id] = {
            "connected": device.connected,
            "away": device.away,
            "power_limit": device.power_limit,
            "home_id": device.home_id,
            "nodes": {
                key: {
                    "status": node.status,
                    "setup": node.setup,
                    "prog": node.prog,
                    "version": node.version,
                    "available": node.available,
                }
                for key, node in device.node_states.items()
            },
        }
    return {
        "devices": devices,
        # Which node is about to flip Unavailable, and why - the direct answer
        # to "why is this entity unavailable?".
        "armed_deadlines": [
            {
                "dev_id": deadline.dev_id,
                "node_key": deadline.node_key,
                "kind": deadline.kind,
                "slot": deadline.slot,
                "expires_in": round(deadline.expires_in, 3),
            }
            for deadline in runtime.engine.deadlines
        ],
    }

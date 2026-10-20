"""Provide info to system health.

One entry per integration: the API's own health/version endpoints
(unauthenticated GETs over the entry's client) and the reseller web
reachability. Facts read None while the entry is failing/retrying setup.
"""

from typing import TYPE_CHECKING, Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .reseller import reseller_from_entry_data
from .smartbox_protocol.errors import SmartboxError

if TYPE_CHECKING:
    from . import SmartboxConfigEntry


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the info page."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        # No config entry set up yet: nothing to report
        return {}
    # Only a LOADED entry has a live runtime. A failed/retrying entry can
    # still carry a torn-down runtime_data object; reporting its client as
    # healthy would be wrong.
    loaded = hass.config_entries.async_loaded_entries(DOMAIN)
    config_entry: SmartboxConfigEntry | None = next(
        (
            entry
            for entry in loaded
            if getattr(entry, "runtime_data", None) is not None
        ),
        None,
    )
    if config_entry is None:
        return {
            "api_version": None,
            "api_health_check": None,
            "can_reach_web_reseller": None,
        }
    runtime = getattr(config_entry, "runtime_data", None)
    if runtime is None:
        # A failing/retrying entry has no client yet; report unknowns.
        return {
            "api_version": None,
            "api_health_check": None,
            "can_reach_web_reseller": None,
        }
    reseller = (
        runtime.identity.devices[0].reseller
        if runtime.identity.devices
        else reseller_from_entry_data(config_entry.data)
    )
    # HA wraps this callback in a short timeout, so a failing read reports
    # its reason here instead of collapsing into a generic failure.
    try:
        api_version = await runtime.client.get_api_version()
        version_text: str | None = ".".join(
            str(api_version[key])
            for key in ("major", "minor", "subminor", "commit")
        )
    except (SmartboxError, KeyError, TypeError) as ex:
        version_text = f"error: {type(ex).__name__}"
    try:
        health_text: str | None = str((await runtime.client.health_check())["message"])
    except (SmartboxError, KeyError, TypeError) as ex:
        health_text = f"error: {type(ex).__name__}"
    return {
        "api_version": version_text,
        "api_health_check": health_text,
        "can_reach_web_reseller": (
            system_health.async_check_can_reach_url(hass, reseller.web_url)
            if reseller.web_url
            else None
        ),
    }


@callback
def async_register(
    _: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)

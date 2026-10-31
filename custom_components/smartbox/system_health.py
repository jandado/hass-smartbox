"""Provide info to system health.

The API's own health/version endpoints (unauthenticated GETs over each entry's
client) and the reseller web reachability. A single loaded entry keeps the
original flat facts; multiple entries report an account count plus one joined
value per fact, so a multi-account install no longer shows only the first
entry. Facts read None while every entry is failing/retrying setup.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .reseller import reseller_from_entry_data
from .smartbox_protocol.errors import SmartboxError

if TYPE_CHECKING:
    from . import SmartboxConfigEntry, SmartboxRuntime


def _reseller_url(runtime: SmartboxRuntime, entry: SmartboxConfigEntry) -> str | None:
    """Return the reseller portal URL for an entry's runtime."""
    if runtime.identity.devices:
        return runtime.identity.devices[0].reseller.web_url
    return reseller_from_entry_data(entry.data).web_url


async def _api_facts(runtime: SmartboxRuntime) -> tuple[str | None, str | None]:
    """Read one runtime's API version and health, mapping failures to text."""
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
    return version_text, health_text


async def _all_resellers_reachable(hass: HomeAssistant, urls: list[str]) -> str:
    """Check every distinct reseller portal; ``ok`` only when all answer."""
    results = await asyncio.gather(
        *(system_health.async_check_can_reach_url(hass, url) for url in urls)
    )
    failures = sum(result != "ok" for result in results)
    return "ok" if not failures else f"failed: {failures}"


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the info page."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        # No config entry set up yet: nothing to report
        return {}

    loaded_entries = hass.config_entries.async_loaded_entries(DOMAIN)
    runtimes = [
        (entry, runtime)
        for entry in loaded_entries
        if (runtime := getattr(entry, "runtime_data", None)) is not None
    ]
    if not runtimes:
        # Every entry is failing/retrying: no live client to ask.
        return {
            "api_version": None,
            "api_health_check": None,
            "can_reach_web_reseller": None,
        }

    if len(loaded_entries) == 1:
        entry, runtime = runtimes[0]
        version_text, health_text = await _api_facts(runtime)
        url = _reseller_url(runtime, entry)
        return {
            "api_version": version_text,
            "api_health_check": health_text,
            "can_reach_web_reseller": (
                system_health.async_check_can_reach_url(hass, url) if url else None
            ),
        }

    versions: list[str] = []
    healths: list[str] = []
    urls: set[str] = set()
    for entry, runtime in runtimes:
        version_text, health_text = await _api_facts(runtime)
        versions.append(f"{entry.title}: {version_text}")
        healths.append(f"{entry.title}: {health_text}")
        if url := _reseller_url(runtime, entry):
            urls.add(url)
    return {
        "accounts": len(loaded_entries),
        "unavailable_accounts": len(loaded_entries) - len(runtimes),
        "api_version": "; ".join(versions),
        "api_health_check": "; ".join(healths),
        "can_reach_web_reseller": (
            _all_resellers_reachable(hass, sorted(urls)) if urls else None
        ),
    }


@callback
def async_register(
    hass: HomeAssistant,  # noqa: ARG001 - HA callback signature
    register: system_health.SystemHealthRegistration,
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)

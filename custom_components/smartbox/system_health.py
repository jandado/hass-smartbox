"""Provide info to system health.

The API's own health/version endpoints (unauthenticated GETs over each entry's
client) and the reseller web reachability. Each fact has its own timeout and
failure text, so one slow or broken read cannot blank the whole page. A single
loaded entry keeps the flat facts; multiple entries report an account count
plus one joined value per fact, so a multi-account install no longer shows only
the first entry. Per-account labels use the reseller name and an index, never
the entry title, which contains the account's username/e-mail.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .reseller import reseller_from_entry_data

if TYPE_CHECKING:
    from smartbox.protocol.reseller import SmartboxReseller

    from . import SmartboxConfigEntry, SmartboxRuntime


def _reseller(runtime: SmartboxRuntime, entry: SmartboxConfigEntry) -> SmartboxReseller:
    """Return the account's reseller for labels and portal checks."""
    if runtime.identity.devices:
        return runtime.identity.devices[0].reseller
    return reseller_from_entry_data(entry.data)


def _reseller_url(runtime: SmartboxRuntime, entry: SmartboxConfigEntry) -> str | None:
    """Return the reseller portal URL for an entry's runtime."""
    return _reseller(runtime, entry).web_url


def _account_label(
    index: int, runtime: SmartboxRuntime, entry: SmartboxConfigEntry
) -> str:
    """Non-identifying label for one configured account."""
    return f"{_reseller(runtime, entry).name} #{index}"


_API_FACT_TIMEOUT = 2.0


async def _api_version(runtime: SmartboxRuntime) -> str:
    """Read one runtime's API version, mapping failures to text."""
    try:
        async with asyncio.timeout(_API_FACT_TIMEOUT):
            api_version = await runtime.client.get_api_version()
            return ".".join(
                str(api_version[key])
                for key in ("major", "minor", "subminor", "commit")
            )
    except Exception as ex:  # noqa: BLE001 - every failure becomes a visible value
        return f"error: {type(ex).__name__}"


async def _api_health(runtime: SmartboxRuntime) -> str:
    """Read one runtime's health endpoint, mapping failures to text."""
    try:
        async with asyncio.timeout(_API_FACT_TIMEOUT):
            return str((await runtime.client.health_check())["message"])
    except Exception as ex:  # noqa: BLE001 - every failure becomes a visible value
        return f"error: {type(ex).__name__}"


async def _api_facts(runtime: SmartboxRuntime) -> tuple[str, str]:
    """Read both facts together; each has its own timeout and failure text."""
    version_text, health_text = await asyncio.gather(
        _api_version(runtime), _api_health(runtime)
    )
    return version_text, health_text


async def _safe_facts(runtime: SmartboxRuntime) -> tuple[str, str]:
    """Contain one account's unexpected failure from the other accounts."""
    try:
        return await _api_facts(runtime)
    except Exception as ex:  # noqa: BLE001 - one account must not blank others
        text = f"error: {type(ex).__name__}"
        return text, text


async def _all_resellers_reachable(hass: HomeAssistant, urls: list[str]) -> str:
    """Check every distinct reseller portal; ``ok`` only when all answer."""
    results = await asyncio.gather(
        *(system_health.async_check_can_reach_url(hass, url) for url in urls)
    )
    failures = sum(result != "ok" for result in results)
    return "ok" if not failures else f"failed: {failures}"


def _all_none() -> dict[str, Any]:
    return {
        "api_version": None,
        "api_health_check": None,
        "can_reach_web_reseller": None,
    }


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the info page."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.disabled_by is None
    ]
    if not entries:
        # No enabled config entry: nothing to report
        return {}
    entry_index = {
        entry.entry_id: index for index, entry in enumerate(entries, start=1)
    }

    loaded_entries = hass.config_entries.async_loaded_entries(DOMAIN)
    runtimes = [
        (entry, runtime)
        for entry in loaded_entries
        if (runtime := getattr(entry, "runtime_data", None)) is not None
    ]
    if not runtimes:
        # Every entry is failing/retrying: no live client to ask.
        info = _all_none()
        if len(entries) > 1:
            info["accounts"] = len(entries)
            info["unavailable_accounts"] = len(entries)
        return info

    if len(entries) == 1:
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

    facts = await asyncio.gather(*(_safe_facts(runtime) for _, runtime in runtimes))
    versions: list[str] = []
    healths: list[str] = []
    urls: set[str] = set()
    for (entry, runtime), (version_text, health_text) in zip(
        runtimes, facts, strict=True
    ):
        label = _account_label(entry_index.get(entry.entry_id, 1), runtime, entry)
        versions.append(f"{label}: {version_text}")
        healths.append(f"{label}: {health_text}")
        if url := _reseller_url(runtime, entry):
            urls.add(url)
    return {
        "accounts": len(entries),
        "unavailable_accounts": len(entries) - len(runtimes),
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

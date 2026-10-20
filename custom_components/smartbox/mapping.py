"""Wire mapping helpers: the status/setup-shaped rules the entities share.

Pure functions the platforms call at runtime: the setpoint/mode/preset decision,
schedule normalization and slot math, RTC and boost time decoding, unit
resolution, service-target resolution. Single-source, so the same conversion is
not re-implemented per platform, with the wire rules cited from
``docs/api-contract.md``.

The write decisions return an INTENT (which named operation, with domain
values), not a wire body: the body belongs to
:mod:`smartbox_protocol.writes`, which owns every wire shape the API accepts.

Entity availability gates live in :mod:`custom_components.smartbox.identity`:
they run on the discovery identity objects, not on wire dicts.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.climate import (
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    HVACMode,
)
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    UnitOfTemperature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    GITHUB_ISSUES_URL,
    PRESET_FROST,
    PRESET_SCHEDULE,
    PRESET_SELF_LEARN,
    SmartboxNodeType,
)
from .smartbox_protocol.values import bounded_repr

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant

    from . import SmartboxRuntime

# A programme day always covers 24 h: slot length = 1440 / len(day array).
_PROG_DAY_MINUTES = 1440


ProgDict = dict[str, list[int]]


@dataclass(frozen=True)
class SetpointWrite:
    """A plain setpoint: the ``set_setpoint`` operation, in this scale.

    Applies immediately; inside ``auto`` it does not persist the override
    across programme transitions (see :class:`AutoOverrideWrite`).
    """

    units: str


@dataclass(frozen=True)
class AutoOverrideWrite:
    """The app-shaped override: ``engage_modified_auto``, in this scale."""

    units: str


@dataclass(frozen=True)
class HtrModWrite:
    """An htr_mod status change: the keys it sets (``None`` = not set).

    The key set is the app's htr_mod one (docs/api-contract.md §7 item 7), and
    the operation sends only the keys that are not ``None``. ``comfort_temp`` is
    a temperature in the given ``units``: the protocol layer formats it onto the
    setpoint grid.
    """

    on: bool | None = None
    mode: str | None = None
    selected_temp: str | None = None
    comfort_temp: float | None = None
    eco_offset: str | None = None
    units: str | None = None


@dataclass(frozen=True)
class ModeWrite:
    """A plain mode change: the ``set_mode`` operation and its mode."""

    mode: str


TemperatureWrite = SetpointWrite | AutoOverrideWrite | HtrModWrite
StatusModeWrite = ModeWrite | HtrModWrite


def get_temperature_unit(status: dict) -> UnitOfTemperature | None:
    """Get the unit of temperature; an unknown/ill-typed value reads as None.

    Entity properties call this while rendering state, so malformed
    server data must not raise there; the caller falls back to Celsius.
    """
    if "units" not in status:
        return None
    unit = status["units"]
    if unit == "C":
        return UnitOfTemperature.CELSIUS
    if unit == "F":
        return UnitOfTemperature.FAHRENHEIT
    return None


def rtc_time_to_datetime(rtc: dict | None) -> datetime | None:
    """Convert a device RTC payload into a local datetime, or None.

    Wire shape: ``{"d": 27, "h": 9, "m": 55, "n": 8, "s": 24, "w": 0, "y":
    2026}``. September arrives as ``n=8``, so ``n`` is a 0-indexed month
    (unconfirmed for December, where a 1-indexed 12 fails to parse and yields
    None). The RTC is read in the Home Assistant timezone; the box timezones
    the cloud reports are not captured.
    """
    if rtc is None:
        return None
    try:
        return datetime(
            year=int(rtc["y"]),
            month=int(rtc["n"]) + 1,
            day=int(rtc["d"]),
            hour=int(rtc["h"]),
            minute=int(rtc["m"]),
            second=int(rtc["s"]),
            tzinfo=dt_util.DEFAULT_TIME_ZONE,
        )
    except (KeyError, TypeError, ValueError):
        return None


def get_boost_end_datetime(boost_end_min: int) -> datetime | None:
    """Return the local datetime at which the node boost ends.

    boost_end_min is the minute of the day (UTC) at which the boost ends, as
    reported by the API status. Returns a future datetime (the next day if the
    boost already ended earlier in the current UTC day), or None for a
    malformed/out-of-range minute value (entity properties must not raise).
    """
    try:
        minute_of_day = int(boost_end_min)
    except (TypeError, ValueError):
        return None
    if not 0 <= minute_of_day < _PROG_DAY_MINUTES:
        return None
    now = dt_util.utcnow()
    hour, minute = divmod(minute_of_day, 60)
    boost_end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if boost_end <= now:
        # Boost already ended for the current UTC day: it ends tomorrow.
        boost_end += timedelta(days=1)
    return dt_util.as_local(boost_end)


def check_status_key(key: str, node_type: str, status: dict) -> None:
    """Raise KeyError for a required status key (report-link message).

    Cross-module contract: climate.py imports this for its preset gates.
    """
    if key not in status:
        # keep the key + node identity; never embed the status dict
        # (server-controlled, can be huge)
        msg = f"'{key}' not found in {node_type} - please report to {GITHUB_ISSUES_URL}."
        raise KeyError(msg)


def get_target_temperature(node_type: str, status: dict) -> float | None:
    """Get the target temperature.

    None when the node is off, matching the official app. The wire keeps the
    stale previous-mode value, so the off check mirrors ``get_hvac_mode``
    instead of trusting ``stemp`` / ``selected_temp``.
    """
    if node_type == SmartboxNodeType.HTR_MOD:
        if (
            status.get("mode") == "off"
            or status.get("on", True) is False
            or status.get("selected_temp") == "off"
        ):
            return None
        check_status_key("selected_temp", node_type, status)
        if status["selected_temp"] == "comfort":
            check_status_key("comfort_temp", node_type, status)
            return float(status["comfort_temp"])
        if status["selected_temp"] == "eco":
            check_status_key("comfort_temp", node_type, status)
            check_status_key("eco_offset", node_type, status)
            return float(status["comfort_temp"]) - float(status["eco_offset"])
        if status["selected_temp"] == "ice":
            check_status_key("ice_temp", node_type, status)
            return float(status["ice_temp"])
        msg = (
            f"Unexpected 'selected_temp' value {bounded_repr(status['selected_temp'])}"
            f" found for {node_type} - please report to"
            f" {GITHUB_ISSUES_URL}."
        )
        raise KeyError(msg)
    if status.get("mode") == "off":
        return None
    check_status_key("stemp", node_type, status)
    return float(status["stemp"])


def temperature_write(
    node_type: str,
    status: dict,
    temp: float,
    setup: dict | None = None,
) -> TemperatureWrite:
    """Map a target-temperature change onto the operation that performs it."""
    check_status_key("units", node_type, status)
    if node_type == SmartboxNodeType.HTR_MOD:
        if status["selected_temp"] == "comfort":
            target_temp = temp
        elif status["selected_temp"] == "eco":
            check_status_key("eco_offset", node_type, status)
            target_temp = temp + float(status["eco_offset"])
        elif status["selected_temp"] == "ice":
            msg = "Can't set temperature for htr_mod devices when ice mode is selected"
            raise ValueError(msg)
        else:
            msg = (
                f"Unexpected 'selected_temp' value {bounded_repr(status['selected_temp'])}"
                f" found for {node_type} - please report to "
                f"{GITHUB_ISSUES_URL}."
            )
            raise KeyError(msg)
        return HtrModWrite(
            on=True,
            mode=status["mode"],
            selected_temp=status["selected_temp"],
            comfort_temp=target_temp,
            eco_offset=status["eco_offset"],
            units=status["units"],
        )
    # Setpoint inside the running programme: the official app engages
    # "modified_auto" with the same body so the override survives programme
    # transitions (docs/api-contract.md §5.1; live-probed 2026-09-28: the body
    # flips the mode on the wire, the heater follows, and it reverts with
    # {"mode": "auto"}). The app's INDEPENDENT_TEMP_AND_MODE_ON_UPDATE
    # capability is not wire-visible; modified_auto_span in setup is the
    # available proxy for "this unit supports the feature".
    if (
        setup is not None
        and "modified_auto_span" in setup
        and status.get("mode") == "auto"
    ):
        return AutoOverrideWrite(units=status["units"])
    return SetpointWrite(units=status["units"])


def get_hvac_mode(node_type: str, status: dict) -> HVACMode | None:
    """Get the mode of HVAC."""
    if status.get("boost", False):
        return HVACMode.HEAT
    check_status_key("mode", node_type, status)
    if status["mode"] == "off" or (
        node_type == SmartboxNodeType.HTR_MOD and not status.get("on", True)
    ):
        return HVACMode.OFF
    mapped = {
        "manual": HVACMode.HEAT,
        "auto": HVACMode.AUTO,
        "modified_auto": HVACMode.AUTO,
        "self_learn": HVACMode.AUTO,
        "presence": HVACMode.AUTO,
        # Vendor-app modes this integration does not model: keep the entity
        # usable rather than raising while a vendor-app change lands.
        "easy": HVACMode.HEAT,
        "setback": HVACMode.HEAT,
    }.get(status["mode"])
    if mapped is not None:
        return mapped
    msg = f"Unknown smartbox node mode {status['mode']}"
    raise ValueError(msg)


def hvac_mode_write(
    node_type: str, status: dict, hvac_mode: str
) -> StatusModeWrite:
    """Map an HVAC-mode change onto the operation that performs it."""
    error_msg = f"Unsupported hvac mode {hvac_mode}"
    if node_type == SmartboxNodeType.HTR_MOD:
        if hvac_mode == HVACMode.OFF:
            return HtrModWrite(on=False)
        if hvac_mode == HVACMode.HEAT:
            # The status key has to travel with the mode on these nodes
            check_status_key("selected_temp", node_type, status)
            return HtrModWrite(
                on=True, mode="manual", selected_temp=status["selected_temp"]
            )
        if hvac_mode == HVACMode.AUTO:
            return HtrModWrite(on=True, mode="auto")
        raise ValueError(error_msg)
    if hvac_mode == HVACMode.OFF:
        return ModeWrite(mode="off")
    if hvac_mode == HVACMode.HEAT:
        return ModeWrite(mode="manual")
    if hvac_mode == HVACMode.AUTO:
        return ModeWrite(mode="auto")
    raise ValueError(error_msg)


def preset_mode_write(node_type: str, status: dict, preset_mode: str) -> StatusModeWrite:
    """Map a preset change onto the operation that performs it."""
    if node_type != SmartboxNodeType.HTR_MOD:
        msg = f"{node_type} nodes do not support preset {preset_mode}"
        raise ValueError(msg)
    # PRESET_HOME and PRESET_AWAY are not handled via status updates
    if preset_mode in (PRESET_HOME, PRESET_AWAY, PRESET_NONE):
        msg = f"Preset {preset_mode} is registry-only, not a status update"
        raise ValueError(msg)

    if preset_mode == PRESET_SCHEDULE:
        return hvac_mode_write(node_type, status, HVACMode.AUTO)
    if preset_mode == PRESET_SELF_LEARN:
        return HtrModWrite(on=True, mode="self_learn")
    if preset_mode == PRESET_ACTIVITY:
        return HtrModWrite(on=True, mode="presence")
    if preset_mode == PRESET_COMFORT:
        return HtrModWrite(on=True, mode="manual", selected_temp="comfort")
    if preset_mode == PRESET_ECO:
        return HtrModWrite(on=True, mode="manual", selected_temp="eco")
    if preset_mode == PRESET_FROST:
        return HtrModWrite(on=True, mode="manual", selected_temp="ice")
    msg = f"Unsupported preset {preset_mode} for node type {node_type}"
    raise ValueError(msg)


def get_current_prog_profile(prog: ProgDict, now: datetime) -> int | None:
    """Return the profile index the schedule asks for at local time ``now``.

    Day keys "0".."6" run Monday..Sunday. Slot length is 24 h divided by the
    day-array length (24 hourly slots, or 48 half-hourly where supported).
    """
    day = prog.get(str(now.weekday()))
    if not day:
        return None
    slot_minutes = _PROG_DAY_MINUTES // len(day)
    if slot_minutes < 1:
        return None
    slot = (now.hour * 60 + now.minute) // slot_minutes
    return day[min(slot, len(day) - 1)]


def get_next_prog_change(prog: ProgDict, now: datetime) -> datetime | None:
    """Return the local datetime at which the schedule next changes profile.

    Walks slot boundaries forward from ``now``, wrapping after Sunday; None
    when the programme never changes. ``now`` must be an aware datetime (its
    timezone defines "local"); DST shifts are handled best-effort.
    """
    current = get_current_prog_profile(prog, now)
    local_now = dt_util.as_local(now)
    for day_offset in range(8):
        day = prog.get(str((local_now.weekday() + day_offset) % 7))
        if not day:
            continue
        slot_minutes = _PROG_DAY_MINUTES // len(day)
        if slot_minutes < 1:
            continue
        day_start = (local_now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for slot, profile in enumerate(day):
            start = dt_util.as_local(
                day_start + timedelta(minutes=slot * slot_minutes)
            )
            if start <= local_now:
                continue
            if profile != current:
                return start
    return None


def resolve_target_entity_ids(hass: HomeAssistant, data: dict) -> set[str]:
    """Resolve a service call's targets (entity/device/area) to entity ids."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    target_entity_ids: set[str] = set(data.get(ATTR_ENTITY_ID, []))
    for device_id in data.get(ATTR_DEVICE_ID, []):
        target_entity_ids.update(
            entity.entity_id
            for entity in er.async_entries_for_device(entity_registry, device_id)
        )
    for area_id in data.get(ATTR_AREA_ID, []):
        for device in dr.async_entries_for_area(device_registry, area_id):
            target_entity_ids.update(
                entity.entity_id
                for entity in er.async_entries_for_device(
                    entity_registry, device.id
                )
            )
    return target_entity_ids


def require_service_target(hass: HomeAssistant, data: dict, service: str) -> set[str]:
    """Resolve a service call's targets, or raise when there is none.

    Both Smartbox services act only on the call's target, and Home Assistant
    does not require one for a manually registered service. Without this a call
    would iterate nothing and report success - a silent no-op.
    """
    target_entity_ids = resolve_target_entity_ids(hass, data)
    if not target_entity_ids:
        msg = (
            f"{service} needs at least one target (entity, device or area); "
            "none of the call's targets resolved to a Smartbox entity"
        )
        raise HomeAssistantError(msg)
    return target_entity_ids


def remove_service(hass: HomeAssistant, service: str) -> None:
    """Drop a service a platform registered, if it is still there.

    A manually registered service outlives the entities it acts on, so after
    unload it would keep accepting calls and silently skip every target.
    Guarded, so unloading one entry cannot raise over another entry's service
    of the same name.
    """
    if hass.services.has_service(DOMAIN, service):
        hass.services.async_remove(DOMAIN, service)


def loaded_runtimes(hass: HomeAssistant) -> Iterator[SmartboxRuntime]:
    """Yield the runtime of every loaded config entry of this domain.

    The domain services are registered once and act across entries, so their
    handlers resolve targets against every loaded entry's entities - not only
    the entry that happened to register the handler.
    """
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            yield runtime


def remove_service_if_last(hass: HomeAssistant, service: str) -> None:
    """Remove a domain service once no loaded entry needs it.

    Runs from an entry's on-unload, at which point the unloading entry no
    longer counts as loaded: a surviving loaded entry keeps the service (and
    its handler) alive.
    """
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        remove_service(hass, service)

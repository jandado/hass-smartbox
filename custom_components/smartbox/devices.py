"""Home Assistant device-registry glue: the box device and the node links.

The box (the gateway the heaters talk through) is its own HA device. Node
devices are created by their entities' ``device_info``; this module links each
one to its box through the registry relationship HA renders as "Connected
devices" on the box page and "Connected via" on the node page.

Home Assistant 2026.8 replaced the identifier-based ``via_device`` with the
registry-id-based ``via_device_id`` (``via_device`` is removed in 2027.8). The
integration's floor is 2026.5, so :func:`node_link` selects the input the
running release accepts. The probe is the registry lookup that arrived with
the new field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant

    from .identity import SmartboxDeviceInfo

__all__ = ["async_ensure_box_devices", "node_link"]

# HA 2026.8 added via_device_id and this entry-scoped lookup, and deprecated
# via_device; the 2026.5 floor only has the identifier tuple.
_HAS_VIA_DEVICE_ID = hasattr(dr.DeviceRegistry, "async_get_device_by_identifier")


def async_ensure_box_devices(
    hass: HomeAssistant,
    entry_id: str,
    devices: Iterable[SmartboxDeviceInfo],
) -> dict[str, str]:
    """Create or refresh the box device for each discovered Smartbox device.

    Returns dev_id -> device registry id. Idempotent: setup calls it before
    the platforms attach entities, and the membership refresh calls it for
    boxes discovered later, so a box entry exists before any of its node
    entities is registered. Display fields are written both here and by the
    box entities' device_info.
    """
    registry = dr.async_get(hass)
    return {
        device.dev_id: registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, device.dev_id)},
            manufacturer=device.reseller.name,
            name=device.name,
            model_id=str(device.model_id),
            sw_version=str(device.sw_version),
            serial_number=str(device.serial_number),
            configuration_url=device.configuration_url,
        ).id
        for device in devices
    }


def node_link(hass: HomeAssistant, entry_id: str, dev_id: str) -> DeviceInfo:
    """Return the device-info fragment linking a node device to its box.

    On HA 2026.8+ this is the box's registry id; older releases get the
    identifier tuple the box entry was created with. An absent box (a node
    materialized before its box entry) yields no link instead of failing the
    entity add; the next add repairs it.
    """
    if not _HAS_VIA_DEVICE_ID:
        # via_device left the typed DeviceInfo when it was deprecated; the
        # 2026.5-2026.7 releases still consume it as the identifier input.
        return cast("DeviceInfo", {"via_device": (DOMAIN, dev_id)})
    box = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, dev_id), entry_id)
    return DeviceInfo(via_device_id=box.id) if box is not None else DeviceInfo()

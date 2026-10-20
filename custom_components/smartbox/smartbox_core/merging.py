"""Wire-shape rules the merge path applies to raw payloads.

Everything here is a pure function of its arguments: it reads a wire payload
(and, for the diffs, the stored slot it is compared against) and returns what
the store should take. State, tasks and notification belong to
:mod:`smartbox_core.engine`; the typed accessors the entities read live in
:mod:`smartbox_protocol.values`.

The shapes encoded here are the ones ``docs/api-contract.md`` records: absent
or ill-typed slots never clear a stored value, the box power limit arrives in
two copies with different types (§9, E17/E18), and a node entry is keyed by
``type`` / ``addr``.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from ..smartbox_protocol.values import BoostConfig, is_lost, prog_days, same_wire_value
from .state import NODE_SLOTS, DeviceField, DeviceState, NodeState, node_key

__all__ = [
    "DEVICE_UPDATE_PATHS",
    "MALFORMED",
    "boost_config",
    "coerce_power_limit",
    "device_slots_delta",
    "device_update_value",
    "entry_key",
    "snapshot_slot_updates",
    "valid_power_limit",
    "write_intent",
]

_LOGGER = logging.getLogger(__name__)

# device-level update path -> DeviceField slot (the body shape is
# resolved per slot by device_update_value)
DEVICE_UPDATE_PATHS: dict[tuple[str, ...], DeviceField] = {
    ("connected",): "connected",
    ("mgr", "away_status"): "away",
    ("htr_system", "power_limit"): "power_limit",
    # A box-limit write pushes the INT copy through this frame as well, and a
    # "0" clear pushes ONLY this one (live 2026-10-18), so the key lands in
    # the same numeric slot; the frame's other htr_system setup keys are
    # unread.
    ("htr_system", "setup"): "power_limit",
}

# Returned by device_update_value for a body that cannot become a slot value.
MALFORMED = object()


def _copy_dict(value: object) -> dict[str, Any] | None:
    """Copy a wire dict payload; anything else means absent/ill-typed."""
    return dict(value) if isinstance(value, Mapping) else None


def boost_config(setup: Mapping[str, Any]) -> int:
    """Return the node's ``factory_options.boost_config`` (0 when absent).

    The vendor app's enum is 0 unsupported, 1 max-temp-fixed-time,
    2 set-temp-and-time, 3 full. Compared numerically so an unknown future
    value reads as "advertises boost" instead of raising while an entity
    state is written.
    """
    factory = setup.get("factory_options")
    value = factory.get("boost_config") if isinstance(factory, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return BoostConfig.UNSUPPORTED
    return int(value)


def entry_key(entry: Mapping[str, Any], dev_id: str) -> str | None:
    """Canonical map key for a wire node entry; ``None`` when malformed."""
    node_type = entry.get("type")
    addr = entry.get("addr")
    if not isinstance(node_type, str) or not node_type or addr is None:
        # The wire key is ``type``; the vendored layers read what the server
        # sends, so a node entry keyed ``node_type`` is not a node.
        _LOGGER.warning("Node entry missing type/addr on %r; ignoring", dev_id)
        return None
    return node_key(node_type, str(addr))


def device_slots_delta(
    device: DeviceState,
    dev_data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[DeviceField]]:
    """Diff the scalar device slots in a devData payload against the store.

    Absent/ill-typed slots are skipped (a snapshot never clears a slot by
    omission). Returns the field updates and the changed kinds.
    """
    updates: dict[str, Any] = {}
    kinds: list[DeviceField] = []
    connected = dev_data.get("connected")
    if isinstance(connected, bool) and connected != device.connected:
        updates["connected"] = connected
        kinds.append("connected")
    away = _copy_dict(dev_data.get("away_status"))
    if away and away != device.away:
        updates["away"] = away
        kinds.append("away")
    htr_system = _copy_dict(dev_data.get("htr_system"))
    if htr_system:
        power_limit = _snapshot_power_limit(htr_system)
        if power_limit is not None and power_limit != device.power_limit:
            updates["power_limit"] = power_limit
            kinds.append("power_limit")
    return updates, kinds


def coerce_power_limit(value: object) -> object:
    """Return the digit-string wire form of a power limit as an int.

    The write and its update frame carry ``{"power_limit": "<int as string>"}``
    (E18) while the snapshot's ``setup`` copy is an int. The slot serves one
    numeric value (E17), so the number entity and the dedup never see a type
    flip.
    """
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def valid_power_limit(value: object) -> int | None:
    """Return a usable integer power limit, or ``None`` for malformed wire data."""
    coerced = coerce_power_limit(value)
    if isinstance(coerced, bool) or not isinstance(coerced, int):
        return None
    if coerced < 0:
        # The write API refuses negative limits; a frame carrying one is
        # malformed, not a setting.
        return None
    return coerced


def _snapshot_power_limit(htr_system: Mapping[str, Any]) -> object | None:
    """Extract the box power limit from a ``devData.htr_system`` subtree.

    Both copies are present and kept in sync: ``power_limit.power_limit`` as
    the digit string ``'3600'`` and ``setup.power_limit`` as the int ``3600``.
    The vendor web client reads the object copy, the mobile client falls back
    to ``setup`` when it is empty, so that order is used here. A bare scalar
    under ``power_limit`` (older families) counts as the object copy, and an
    unreported limit stays ``None`` so the slot keeps its value.
    """
    limit = htr_system.get("power_limit")
    if isinstance(limit, Mapping):
        limit = limit.get("power_limit")
    if (usable := valid_power_limit(limit)) is not None:
        return usable
    setup = htr_system.get("setup")
    fallback = setup.get("power_limit") if isinstance(setup, Mapping) else None
    return valid_power_limit(fallback)


def device_update_value(field: DeviceField, body: Mapping[str, Any]) -> Any:  # noqa: ANN401 - raw wire passthrough
    """Resolve an update body into a slot value; ``MALFORMED`` if unusable."""
    if field == "connected":
        connected = body.get("connected")
        if not isinstance(connected, bool):
            return MALFORMED
        return connected
    if field == "power_limit":
        if "power_limit" not in body:
            return MALFORMED
        value = valid_power_limit(body["power_limit"])
        return MALFORMED if value is None else value
    return dict(body)  # the remaining path's body IS the slot value


def snapshot_slot_updates(old: NodeState, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the slot bodies a snapshot node entry changes.

    Absent or ill-typed slots never clear a stored value, and a value-equal
    slot is not an update - the snapshot path diffs where the frame path
    replaces wholesale. A lost-marked slot is availability evidence, never slot
    data, so it cannot blank the last known body either.
    """
    updates: dict[str, Any] = {}
    for slot in NODE_SLOTS:
        value = entry.get(slot)
        # An empty mapping is "no body", not a value: it must not blank the
        # stored slot or count as reachability (docs/api-contract.md §6.6).
        if isinstance(value, Mapping) and value and not is_lost(value):
            copied = dict(value)
            if copied != getattr(old, slot):
                updates[slot] = copied
    return updates


def write_intent(
    slot: str, current: NodeState, body: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the keys a posted body changes, with the values it sets.

    A setup op posts the whole object but changes one key, so the evidence that
    it landed is the difference against the stored body, not the whole object.
    A prog write takes no key-level evidence (its confirming frame echoes the
    whole week, docs/api-ws-user.md §4), but its day map is still compared
    here: a prog write whose day map is unchanged is a no-op.
    """
    stored = getattr(current, slot)
    if slot == "prog":
        # The store may hold the day map bare or nested under ``prog``; the
        # posted body is always nested. Compare the day maps, not the wrapper:
        # the confirming frame echoes the whole week, and only the changed days
        # are evidence that THIS write landed.
        stored_days = prog_days(stored) if isinstance(stored, Mapping) else None
        body_days = prog_days(body)
        if body_days is None:
            return dict(body)
        if stored_days is None:
            return dict(body_days)
        return {
            key: value
            for key, value in body_days.items()
            if not (key in stored_days and same_wire_value(value, stored_days[key]))
        }
    if not isinstance(stored, Mapping):
        return dict(body)
    return {
        key: value
        for key, value in body.items()
        if not (key in stored and same_wire_value(value, stored[key]))
    }

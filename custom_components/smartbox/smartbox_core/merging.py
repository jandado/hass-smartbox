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

from ..smartbox_protocol.values import is_lost, same_wire_value
from .state import NODE_SLOTS, DeviceField, DeviceState, NodeState, node_key

__all__ = [
    "DEVICE_UPDATE_PATHS",
    "MALFORMED",
    "coerce_power_limit",
    "device_slots_delta",
    "device_update_value",
    "entry_key",
    "snapshot_slot_updates",
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


def prog_days(slot: object) -> dict[str, Any] | None:
    """Return the day-keyed schedule inside a prog slot, or ``None``.

    The slot arrives in both wire shapes: a ``{"prog": {...}}`` frame body and
    a bare day-keyed object (docs/api-contract.md §5.4). ``None`` when it holds
    nothing usable, so a writer falls back to a REST read.
    """
    if not isinstance(slot, Mapping) or not slot:
        return None
    inner = slot.get("prog")
    if isinstance(inner, Mapping):
        return dict(inner) if inner else None
    return dict(slot)


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
    if away is not None and away != device.away:
        updates["away"] = away
        kinds.append("away")
    htr_system = _copy_dict(dev_data.get("htr_system"))
    if htr_system is not None:
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
    if limit is not None:
        return coerce_power_limit(limit)
    setup = htr_system.get("setup")
    fallback = setup.get("power_limit") if isinstance(setup, Mapping) else None
    return coerce_power_limit(fallback) if fallback is not None else None


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
        return coerce_power_limit(body["power_limit"])
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
        if isinstance(value, Mapping) and not is_lost(value):
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
    if not isinstance(stored, Mapping):
        return dict(body)
    return {
        key: value
        for key, value in body.items()
        if not (key in stored and same_wire_value(value, stored[key]))
    }

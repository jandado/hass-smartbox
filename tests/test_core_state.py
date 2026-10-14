"""Tests for the versioned state model (smartbox_core.state).

The model is raw wire state in frozen dataclasses; these tests pin the
shape, the copy-on-write contract surface (frozen + per-instance
dict defaults) and the node-key normalization.
"""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.smartbox.smartbox_core.state import (
    DeviceChange,
    DeviceState,
    NodeChange,
    NodeState,
    WorldState,
    node_key,
)


def test_node_state_defaults() -> None:
    node = NodeState()
    assert node.status == {}
    assert node.setup == {}
    assert node.prog is None
    assert node.version is None
    assert node.revision == 0
    assert node.available is None  # part-2 field, unknown until then


def test_node_state_frozen() -> None:
    node = NodeState(status={"mode": "off"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.revision = 1  # type: ignore[misc]


def test_device_state_defaults_independent() -> None:
    a, b = DeviceState(dev_id="d1"), DeviceState(dev_id="d2")
    a.node_states["htr/1"] = NodeState(revision=3)
    assert b.node_states == {}  # default_factory, not a shared dict


def test_device_state_frozen() -> None:
    device = DeviceState(dev_id="d1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        device.connected = True  # type: ignore[misc]


def test_world_state_defaults_empty() -> None:
    assert WorldState().devices == {}


def test_node_key_normalizes_addr() -> None:
    # snapshot entries may carry a numeric addr, update paths carry it
    # as a path segment - both must land on the same map key
    assert node_key("htr", "2") == "htr/2"
    assert node_key("htr", 2) == "htr/2"
    assert node_key("htr", "2") == node_key("htr", 2)


def test_change_dataclasses_frozen_and_typed() -> None:
    node = NodeState(status={"mode": "heat"}, revision=1)
    change = NodeChange(dev_id="d1", node_key="htr/1", kind="status", node=node)
    with pytest.raises(dataclasses.FrozenInstanceError):
        change.kind = "setup"  # type: ignore[misc]
    assert isinstance(change, NodeChange)
    device = DeviceState(dev_id="d1", revision=2)
    dchange = DeviceChange(dev_id="d1", kind="connected", device=device)
    with pytest.raises(dataclasses.FrozenInstanceError):
        dchange.kind = "away"  # type: ignore[misc]
    assert isinstance(dchange, DeviceChange)

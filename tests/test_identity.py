"""Identity-discovery error contract (``identity.async_discover_identity``).

Plain pytest with an AsyncMock client: pins what discovery does with
drifted/malformed payloads (exact exception TYPES, never messages) and
that protocol-layer errors propagate RAW for setup to map.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.smartbox.const import SmartboxNodeType
from custom_components.smartbox.identity import (
    SmartboxDeviceInfo,
    SmartboxNodeInfo,
    async_discover_identity,
    boost_available,
    boost_config,
    node_power_limit_available,
)
from custom_components.smartbox.smartbox_protocol.errors import (
    SmartboxError,
    TransportUnavailableError,
)
from custom_components.smartbox.smartbox_protocol.values import BoostConfig

HOME = {"id": "h1", "devs": [{"dev_id": "dev1", "name": "Box"}]}
NODE = {"node_type": "htr", "type": "htr", "addr": 2, "name": "Heater"}
PMO_NODE = {"node_type": "pmo", "type": "pmo", "addr": 9, "name": "PMO"}

STATUS = {"mtemp": "190", "units": "C"}
SETUP = {"mode": "auto"}
VERSION = {"pid": "081c", "hw_version": "1.0"}


def make_client(
    homes: object = None,
    nodes: object = None,
) -> AsyncMock:
    """AsyncMock SmartboxClient serving one home + the given nodes list."""
    client = AsyncMock()
    client.get_grouped_devices.return_value = [HOME] if homes is None else homes
    client.get_nodes.return_value = [NODE, PMO_NODE] if nodes is None else nodes
    client.get_node_status.return_value = STATUS
    client.get_node_setup.return_value = SETUP
    client.get_node_version.return_value = VERSION
    return client


# --- happy path anchor + the PMO status skip ---


async def test_discovery_reads_all_node_facts():
    client = make_client()
    identity = await async_discover_identity(client, None)
    assert len(identity.devices) == 1
    assert len(identity.nodes) == 2
    node = identity.nodes[0]
    assert isinstance(node, SmartboxNodeInfo)
    assert isinstance(node.device, SmartboxDeviceInfo)
    assert node.status == STATUS
    assert node.setup == SETUP
    assert node.version == VERSION
    assert client.get_node_status.call_count == 1
    assert client.get_node_setup.call_count == 2
    assert client.get_node_version.call_count == 2


async def test_pmo_node_skips_the_status_read():
    client = make_client()
    await async_discover_identity(client, None)
    called_addrs = [
        call.args[1].addr for call in client.get_node_status.await_args_list
    ]
    assert called_addrs == ["2"]  # PMO (addr 9) never status-polled
    pmo = (await async_discover_identity(client, None)).nodes[1]
    assert pmo.node_type == SmartboxNodeType.PMO
    assert pmo.status == {}


# --- drifted payloads: exact exception types ---


@pytest.mark.parametrize("homes", [{"unexpected": 1}, "nope", 42])
async def test_grouped_devices_not_a_list_raises_smartbox_error(homes):
    client = make_client(homes=homes)
    with pytest.raises(SmartboxError):
        await async_discover_identity(client, None)


@pytest.mark.parametrize(
    ("dev", "expect"),
    [
        ({}, "error"),  # missing dev_id -> SmartboxError
        ({"dev_id": 5}, "error"),  # non-string dev_id -> SmartboxError
        ({"dev_id": ""}, "error"),  # blank dev_id -> SmartboxError
        ("not-a-mapping", "skip"),  # non-mapping entry skipped
    ],
)
async def test_drifted_dev_entries(dev, expect):
    client = make_client(homes=[{"id": "h1", "devs": [dev]}])
    if expect == "error":
        with pytest.raises(SmartboxError):
            await async_discover_identity(client, None)
    else:
        identity = await async_discover_identity(client, None)
        assert identity.devices == ()


async def test_home_without_id_is_skipped():
    client = make_client(homes=[{"no_id": 1}, {"id": "h2", "devs": []}])
    identity = await async_discover_identity(client, None)
    assert identity.devices == ()
    assert client.get_nodes.await_count == 0


async def test_nodes_payload_missing_the_nodes_key_is_defensive():
    # a raw (un-unwrapped) nodes payload: non-Mapping entries are skipped,
    # never a crash from discovery itself
    client = make_client(nodes={"nope": []})
    identity = await async_discover_identity(client, None)
    assert identity.nodes == ()


@pytest.mark.parametrize("slot", ["status", "setup", "version"])
async def test_a_non_object_node_read_fails_closed(slot):
    """A successful read that is not a JSON object is a bounded failure."""
    client = make_client()
    getattr(client, f"get_node_{slot}").return_value = ["not", "an", "object"]
    with pytest.raises(SmartboxError, match=rf"{slot} payload"):
        await async_discover_identity(client, None)


async def test_a_home_with_an_ill_typed_devs_list_fails_closed():
    """A home whose devs is not a list is not silently treated as empty."""
    client = make_client(homes=[{"id": "h1", "devs": None}])
    with pytest.raises(SmartboxError, match="ill-typed devs list"):
        await async_discover_identity(client, None)


async def test_ill_typed_factory_options_do_not_raise():
    """A null/ill-typed factory_options object reads as no options."""
    client = make_client()
    client.get_node_setup.return_value = {"factory_options": None}
    node = (await async_discover_identity(client, None)).nodes[0]
    assert node.boost_available is False


def test_boost_config_uses_the_app_enum():
    """The vendor app's 0/1/2/3 boost_config values are the integration's."""
    assert [int(c) for c in BoostConfig] == [0, 1, 2, 3]
    assert boost_config({"factory_options": {"boost_config": 3}}) == 3
    assert boost_available({"factory_options": {"boost_config": 3}}) is True


@pytest.mark.parametrize(
    "entry",
    [
        {k: v for k, v in NODE.items() if k != "type"},
        {k: v for k, v in NODE.items() if k != "addr"},
        {**NODE, "type": ""},
        {**NODE, "type": 7},
        {**NODE, "addr": None},
        {**NODE, "addr": True},  # a bool is not an address
    ],
)
async def test_node_entry_drift_raises_bounded_smartbox_error(entry):
    """A malformed node entry fails closed with a bounded error.

    A bare ``KeyError`` would escape ``async_setup_entry``'s error mapping, so
    a ``[{}]`` reply produced a traceback instead of a retry. The message must
    stay bounded and payload-free.
    """
    client = make_client(nodes=[entry])
    with pytest.raises(SmartboxError) as err:
        await async_discover_identity(client, None)
    message = str(err.value)
    assert "Node entry missing" in message
    assert "keys=" in message
    assert len(message) < 200


async def test_node_entry_without_a_name_falls_back_to_the_device_name():
    """A name-less node entry must not raise during platform setup.

    The identity's ``name`` feeds the entity's device_info; the device name
    is a safe fallback, where indexing the key would take setup down with a
    bare KeyError.
    """
    client = make_client(nodes=[{"type": "htr", "addr": 2}])
    node = (await async_discover_identity(client, None)).nodes[0]
    assert node.name == "Box"


async def test_get_model_code_from_the_pid():
    """The model code is PID characters 3-4, uppercased (app rule)."""
    for version, expected in (
        ({"pid": "081c", "hw_version": "1.0"}, "1C"),
        ({"pid": "0217"}, "17"),
        ({"pid": "12345"}, "34"),
        ({"pid": "ab"}, None),
        ({"pid": "x"}, None),
        ({"pid": ""}, None),
        ({}, None),
    ):
        client = make_client()
        client.get_node_version.return_value = version
        node = (await async_discover_identity(client, None)).nodes[0]
        assert node.pid == version.get("pid")
        assert node.hw_version == version.get("hw_version")
        assert node.get_model_code() == expected


async def test_get_model_code_uses_the_given_version_payload():
    """A consumer's live version slot overrides the discovery copy."""
    client = make_client()
    client.get_node_version.return_value = {"pid": "0217"}
    node = (await async_discover_identity(client, None)).nodes[0]
    assert node.get_model_code() == "17"
    assert node.get_model_code({"pid": "081c"}) == "1C"
    assert node.get_model_code({}) == "17"  # empty live slot -> discovery
    assert node.get_model_code({"pid": "1C"}) is None  # short PID


# --- protocol-layer errors propagate RAW ---


@pytest.mark.parametrize(
    ("seam", "error"),
    [
        ("get_grouped_devices", TransportUnavailableError("down")),
        ("get_nodes", SmartboxError("no nodes")),
        ("get_node_status", SmartboxError("bad status")),
        ("get_node_setup", SmartboxError("bad setup")),
        ("get_node_version", SmartboxError("bad version")),
    ],
)
async def test_protocol_errors_propagate_raw(seam, error):
    client = make_client()
    getattr(client, seam).side_effect = error
    with pytest.raises(type(error)):
        await async_discover_identity(client, None)


async def test_node_power_limit_gate_reads_the_setup_key():
    """The gate is the setup key the wire carries (observed on pmo nodes)."""
    client = make_client()
    client.get_node_setup.side_effect = [
        {"priority": "low"},  # a heater's setup does not carry the key
        {"power_limit": 1500},  # the pmo node's setup carries it
    ]
    identity = await async_discover_identity(client, None)
    by_type = {node.node_type: node for node in identity.nodes}
    assert node_power_limit_available(by_type[SmartboxNodeType.PMO]) is True
    assert node_power_limit_available(by_type[SmartboxNodeType.HTR]) is False

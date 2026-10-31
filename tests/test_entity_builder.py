"""Tests for the dynamic entity builder registry."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.smartbox.entity_builder import EntityBuilder


def _identity(dev_ids: list[str], node_ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        devices=[SimpleNamespace(dev_id=dev_id) for dev_id in dev_ids],
        nodes=[
            SimpleNamespace(node_id=node_id, dev_id=node_id.split("_")[0])
            for node_id in node_ids
        ],
    )


async def test_builder_adds_new_devices_then_nodes_and_deduplicates():
    """A new box and its node are built once; a repeat is a no-op."""
    builder = EntityBuilder(_identity(["dev_1"], ["dev_1_0"]))
    add_entities = MagicMock()
    builder.register(
        add_entities,
        build_device=lambda device: [([f"box:{device.dev_id}"], False)],
        build_node=lambda node: [([f"node:{node.node_id}"], False)],
    )

    added = await builder.async_add_identity(
        _identity(["dev_1", "dev_2"], ["dev_1_0", "dev_2_1"])
    )

    assert added == (1, 1)
    assert add_entities.call_count == 2
    assert [call.args[0] for call in add_entities.call_args_list] == [
        ["box:dev_2"],
        ["node:dev_2_1"],
    ]
    assert all(
        call.kwargs == {"update_before_add": False}
        for call in add_entities.call_args_list
    )

    added = await builder.async_add_identity(
        _identity(["dev_1", "dev_2"], ["dev_1_0", "dev_2_1"])
    )
    assert added == (0, 0)
    assert add_entities.call_count == 2


async def test_builder_keeps_group_fetch_flags():
    """The total-consumption group's update flag survives the registry."""
    builder = EntityBuilder(_identity([], []))
    add_entities = MagicMock()
    builder.register(
        add_entities,
        build_node=lambda _node: [(["node"], True)],
    )

    await builder.async_add_identity(_identity(["dev_1"], ["dev_1_0"]))

    add_entities.assert_called_once_with(["node"], update_before_add=True)

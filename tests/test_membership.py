"""Tests for the post-setup node membership watch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.smartbox import membership as membership_mod
from custom_components.smartbox.const import DOMAIN, SmartboxNodeType
from custom_components.smartbox.diagnostics import async_get_config_entry_diagnostics
from custom_components.smartbox.identity import async_discover_identity
from custom_components.smartbox.membership import _format_labels, node_added_issue_id
from custom_components.smartbox.smartbox_core.state import NodeAdded


@pytest.fixture(autouse=True)
def _instant_refresh_retries(monkeypatch):
    """Keep the bounded refresh retries instant in the suite."""
    monkeypatch.setattr(membership_mod, "_REFRESH_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


def _snapshot_with_new_node() -> list[dict]:
    """Build a device_1 snapshot carrying its known nodes plus one new htr."""
    return [
        {
            "id": "home_1",
            "devs": [
                {
                    "dev_id": "device_1",
                    "devData": {
                        "connected": True,
                        "away_status": {"away": False},
                        "nodes": [
                            {
                                "type": "htr",
                                "addr": 0,
                                "status": {"stemp": "20.0", "units": "C"},
                                "setup": {"priority": "low"},
                                "version": {"pid": "081c", "hw_version": "2.3"},
                            },
                            {
                                "type": "acm",
                                "addr": 1,
                                "status": {"stemp": "20.0", "units": "C"},
                                "setup": {"priority": "low"},
                                "version": {"pid": "081c", "hw_version": "2.3"},
                            },
                            {
                                "type": "htr",
                                "addr": 2,
                                "name": "Desk Heater",
                                "status": {"stemp": "19.0", "units": "C"},
                                "setup": {"priority": "low"},
                                "version": {"pid": "081c", "hw_version": "2.3"},
                            },
                        ],
                    },
                }
            ],
        }
    ]


async def test_new_node_raises_one_reload_issue_and_reports_it(
    hass, mock_smartbox, config_entry
):
    """A snapshot node missing from REST discovery prompts a reload."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await config_entry.runtime_data.transport.deliver_snapshot(
        _snapshot_with_new_node()
    )
    await hass.async_block_till_done()

    issue_id = (DOMAIN, node_added_issue_id(config_entry))
    issue = ir.async_get(hass).issues[issue_id]
    assert issue.translation_key == "node_added"
    assert issue.translation_placeholders == {"nodes": "Desk Heater"}

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    membership = result["runtime"]["membership"]
    assert membership == {
        "new_nodes": ["Desk Heater"],
        "repair_issue_raised": True,
    }

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue_id not in ir.async_get(hass).issues

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    assert result["runtime"]["membership"] == {
        "new_nodes": [],
        "repair_issue_raised": False,
    }


async def test_new_node_is_materialized_without_reload(
    hass, mock_smartbox, config_entry
):
    """A node present in REST discovery is added in place, with no repair issue."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data

    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_9",
            "fw_version": "fw_version_1_9",
            "serial_id": "serial_id_1_9",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )

    await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
    await hass.async_block_till_done()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues
    assert hass.states.get("climate.desk_heater") is not None
    assert hass.states.get("sensor.desk_heater_power") is not None
    assert any(
        entity.entity_id == "climate.desk_heater" for entity in runtime.services.climate
    )
    assert runtime.history.pending("device_1_2")
    assert any(
        entity.entity_id == "sensor.desk_heater_schedule"
        for entity in runtime.services.schedule
    )
    assert runtime.membership.facts == {
        "new_nodes": [],
        "repair_issue_raised": False,
    }

    # A repeated snapshot must not build the same node twice.
    await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
    await hass.async_block_till_done()
    climate_entities = [
        entity
        for entity in runtime.services.climate
        if entity.entity_id == "climate.desk_heater"
    ]
    assert len(climate_entities) == 1


async def test_new_device_is_materialized_without_reload(
    hass, mock_smartbox, config_entry
):
    """A newly paired box gets its box and node entities in place."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_smartbox.add_device(
        {
            "dev_id": "device_3",
            "name": "Device 3",
            "product_id": "product_id_3",
            "fw_version": "fw_version_3",
            "serial_id": "serial_id_3",
        },
        {
            "addr": 0,
            "name": "Device 3 0",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_3_0",
            "fw_version": "fw_version_3_0",
            "serial_id": "serial_id_3_0",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )

    await config_entry.runtime_data.transport.deliver_snapshot(
        [
            {
                "id": "home_1",
                "devs": [
                    {
                        "dev_id": "device_3",
                        "devData": {
                            "connected": True,
                            "away_status": {"away": False},
                            "nodes": [
                                {
                                    "type": "htr",
                                    "addr": 0,
                                    "status": {"stemp": "20.0", "units": "C"},
                                    "setup": {"priority": "low"},
                                    "version": {"pid": "081c", "hw_version": "2.3"},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    )
    await hass.async_block_till_done()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues
    assert hass.states.get("binary_sensor.device_3_connectivity") is not None
    assert hass.states.get("climate.device_3_0") is not None
    assert hass.states.get("number.device_3_power_limit") is not None


async def test_a_known_node_does_not_raise(hass, mock_smartbox, config_entry):
    """The first snapshot reports the setup-time nodes; none is 'new'."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await config_entry.runtime_data.transport.deliver_snapshot(
        [
            {
                "id": "home_1",
                "devs": [
                    {
                        "dev_id": "device_1",
                        "devData": {
                            "connected": True,
                            "away_status": {"away": False},
                            "nodes": [
                                {
                                    "type": "htr",
                                    "addr": 0,
                                    "status": {"stemp": "20.0", "units": "C"},
                                    "setup": {"priority": "low"},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    )
    await hass.async_block_till_done()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues


async def test_refresh_retries_until_rest_lists_the_node(
    hass, mock_smartbox, config_entry
):
    """A REST response from before the pairing is retried, not surfaced."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_2",
            "fw_version": "fw_version_1_2",
            "serial_id": "serial_id_1_2",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )
    refreshed = await async_discover_identity(
        runtime.client, runtime.identity.devices[0].reseller
    )

    with patch.object(
        membership_mod,
        "async_discover_identity",
        new=AsyncMock(side_effect=[runtime.identity, refreshed]),
    ):
        await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
        await hass.async_block_till_done()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues
    assert hass.states.get("climate.desk_heater") is not None


async def test_pause_defers_and_resume_materializes(hass, mock_smartbox, config_entry):
    """A paused watcher queues membership without touching the platforms."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_2",
            "fw_version": "fw_version_1_2",
            "serial_id": "serial_id_1_2",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )

    await runtime.membership.async_pause()
    await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
    await hass.async_block_till_done()
    assert hass.states.get("climate.desk_heater") is None
    assert runtime.membership.new_nodes

    runtime.membership.resume()
    await hass.async_block_till_done()
    assert hass.states.get("climate.desk_heater") is not None


async def test_close_prevents_new_refresh_tasks(hass, mock_smartbox, config_entry):
    """A closed watcher ignores late membership frames."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_2",
            "fw_version": "fw_version_1_2",
            "serial_id": "serial_id_1_2",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )
    runtime.membership.close()

    await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
    await hass.async_block_till_done()
    assert hass.states.get("climate.desk_heater") is None
    assert runtime.membership.facts["new_nodes"] == []


def test_labels_are_bounded():
    """A server-controlled node name cannot make the issue payload unbounded."""
    rendered = _format_labels(["x" * 500])
    assert "…" in rendered
    assert len(rendered) <= membership_mod._MAX_LABEL_LENGTH


async def test_refresh_failure_falls_back_to_the_reload_issue(
    hass, mock_smartbox, config_entry
):
    """A failing REST refresh surfaces the pending node instead of dying."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_2",
            "fw_version": "fw_version_1_2",
            "serial_id": "serial_id_1_2",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )

    with patch.object(
        membership_mod,
        "async_discover_identity",
        new=AsyncMock(side_effect=RuntimeError("backend down")),
    ):
        await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
        await hass.async_block_till_done()

    issue = ir.async_get(hass).issues[(DOMAIN, node_added_issue_id(config_entry))]
    assert issue.translation_placeholders == {"nodes": "Desk Heater"}
    assert hass.states.get("climate.desk_heater") is None


async def test_pause_cancels_an_in_flight_refresh(hass, mock_smartbox, config_entry):
    """Pause waits for the in-flight refresh before the platforms unload."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_2",
            "fw_version": "fw_version_1_2",
            "serial_id": "serial_id_1_2",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )
    started = asyncio.Event()

    async def slow_discover(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)
        msg = "cancelled refresh should not return"
        raise AssertionError(msg)

    with patch.object(membership_mod, "async_discover_identity", new=slow_discover):
        await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
        await started.wait()
        await runtime.membership.async_pause()
        assert hass.states.get("climate.desk_heater") is None

    runtime.membership.resume()
    await hass.async_block_till_done()
    assert hass.states.get("climate.desk_heater") is not None


async def test_a_transient_refresh_failure_is_retried_with_the_backoff(
    hass, mock_smartbox, config_entry, monkeypatch
):
    """One REST hiccup is retried; the issue waits for the last attempt."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    mock_smartbox.add_node(
        "device_1",
        {
            "addr": 2,
            "name": "Desk Heater",
            "type": SmartboxNodeType.HTR,
            "product_id": "product_id_1_2",
            "fw_version": "fw_version_1_2",
            "serial_id": "serial_id_1_2",
        },
        setup=mock_smartbox._node_setup["device_1"][0],
        status=mock_smartbox._node_status["device_1"][0],
        prog=mock_smartbox._node_prog["device_1"][0],
    )
    refreshed = await async_discover_identity(
        runtime.client, runtime.identity.devices[0].reseller
    )
    delays: list[float] = []

    async def instant_sleep(delay=0):
        delays.append(delay)

    # Rebind only the watcher's reference: the engine's supervision keeps the
    # real seam (an instant global sleep would spin it).
    monkeypatch.setattr(membership_mod, "timing", SimpleNamespace(sleep=instant_sleep))
    monkeypatch.setattr(membership_mod, "_REFRESH_BACKOFF_SECONDS", (5.0, 15.0, 45.0))

    with patch.object(
        membership_mod,
        "async_discover_identity",
        new=AsyncMock(side_effect=[RuntimeError("transient"), refreshed]),
    ):
        await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
        await hass.async_block_till_done()

    assert delays == [0.0, 5.0]
    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues
    assert hass.states.get("climate.desk_heater") is not None


async def test_a_reconcile_failure_raises_the_reload_issue(
    hass, mock_smartbox, config_entry
):
    """An identity that cannot be reconciled surfaces the pending node."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data

    with patch.object(
        runtime.membership, "_reconcile", side_effect=RuntimeError("bad identity")
    ):
        await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
        await hass.async_block_till_done()

    issue = ir.async_get(hass).issues[(DOMAIN, node_added_issue_id(config_entry))]
    assert issue.translation_placeholders == {"nodes": "Desk Heater"}
    assert hass.states.get("climate.desk_heater") is None


async def test_a_pending_node_that_vanished_clears_without_an_issue(
    hass, mock_smartbox, config_entry
):
    """A phantom addition the store no longer holds cannot pin the issue."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    runtime.membership._pending["device_1_9"] = membership_mod._PendingNode(
        label="Ghost Heater", dev_id="device_1", node_key="htr/9"
    )

    runtime.membership._raise_issue()

    assert (DOMAIN, node_added_issue_id(config_entry)) not in ir.async_get(hass).issues
    assert runtime.membership.new_nodes == ()


async def test_resume_without_pending_keeps_the_watcher_idle(
    hass, mock_smartbox, config_entry
):
    """A refused unload that resumes with nothing queued starts no refresh."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data

    runtime.membership.resume()

    assert runtime.membership._refresh_task is None


async def test_a_closed_watcher_stops_before_the_first_attempt(
    hass, mock_smartbox, config_entry
):
    """A close that lands before the loop starts refreshes nothing."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    runtime.membership.close()

    with patch.object(
        membership_mod,
        "async_discover_identity",
        new=AsyncMock(side_effect=AssertionError("must not refresh")),
    ):
        await runtime.membership._async_refresh()

    assert runtime.membership._refresh_task is None


async def test_close_cancels_an_in_flight_refresh(hass, mock_smartbox, config_entry):
    """Unloading the entry stops a refresh that is still reading REST."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    started = asyncio.Event()

    async def slow_discover(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)
        msg = "cancelled refresh should not return"
        raise AssertionError(msg)

    with patch.object(membership_mod, "async_discover_identity", new=slow_discover):
        await runtime.transport.deliver_snapshot(_snapshot_with_new_node())
        await started.wait()
        runtime.membership.close()
        await hass.async_block_till_done()

    assert hass.states.get("climate.desk_heater") is None
    assert runtime.membership._refresh_task is None


async def test_a_new_node_on_an_unknown_device_labels_the_device(
    hass, mock_smartbox, config_entry
):
    """A device the setup never saw is named in the pending label."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data

    await runtime.membership._on_change(
        NodeAdded(dev_id="device_9", node_key="htr/0", name="Attic")
    )
    await runtime.membership.async_pause()

    assert runtime.membership.new_nodes == ("Attic (device_9)",)


def test_more_than_ten_labels_are_summarized():
    """The issue payload bounds how many node names it lists."""
    rendered = _format_labels([f"node {index}" for index in range(12)])
    assert "node 9" in rendered
    assert "node 10" not in rendered
    assert rendered.endswith("…")


def test_non_printable_label_characters_are_dropped():
    assert _format_labels(["Attic\nHeater\x00"]) == "AtticHeater"

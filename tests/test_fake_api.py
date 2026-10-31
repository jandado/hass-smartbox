"""Tests for the stateful reference fake (``tests.fake_api``).

The fake IS the server model: writes mutate the in-memory state exactly
like the wire semantics, GETs return the CURRENT state (so writes are
observable), and every call lands in the ``requests`` log. Composition
with ``FakeTransport`` shows one consistent world.
"""

from __future__ import annotations

import pytest

from custom_components.smartbox.smartbox_protocol.client import NodeRef
from custom_components.smartbox.smartbox_protocol.errors import (
    SmartboxError,
    TransportUnavailableError,
)
from tests.fake_api import FakeSmartbox
from tests.fake_transport import FakeTransport

NODE = NodeRef(node_type="htr", addr="2")
ON = True  # FBT003-friendly bool literals for write calls
SLOTS = list(range(24))


@pytest.fixture
def fake() -> FakeSmartbox:
    box = FakeSmartbox()
    box.add_device("dev1")
    box.add_node(
        "dev1",
        NODE,
        status={"mtemp": "190", "units": "C", "stemp": "20.0", "mode": "auto"},
        setup={
            "priority": "low",
            "htr_brand": "acme",
            "extra_options": {"existing": 1},
        },
        prog={"0": SLOTS, "6": SLOTS},
    )
    return box


# --- construction / reads ---


async def test_get_devices_lists_added_devices():
    box = FakeSmartbox()
    box.add_device("a")
    box.add_device("b")
    assert await box.get_devices() == {"devs": [{"dev_id": "a"}, {"dev_id": "b"}]}


async def test_get_nodes_unwraps_to_client_parity(fake):
    assert await fake.get_nodes("dev1") == [{"type": "htr", "addr": "2"}]


async def test_reads_return_copies_not_internal_state(fake):
    status = await fake.get_node_status("dev1", NODE)
    status["stemp"] = "99.0"
    assert (await fake.get_node_status("dev1", NODE))["stemp"] == "20.0"


async def test_read_unknown_device_raises_smartbox_error(fake):
    with pytest.raises(SmartboxError, match="no node"):
        await fake.get_node_status("nope", NODE)


async def test_add_node_requires_device_first():
    box = FakeSmartbox()
    with pytest.raises(ValueError, match="add_device"):
        box.add_node("ghost", NODE, status={})


# --- write-then-GET round trips (one per op) ---


@pytest.mark.parametrize(
    ("write", "expected_status_keys"),
    [
        (lambda w: w.set_mode("dev1", NODE, "manual"), {"mode": "manual"}),
        (lambda w: w.set_setpoint("dev1", NODE, 21.3, "C"), {"stemp": "21.0"}),
        (
            lambda w: w.engage_modified_auto("dev1", NODE, 22.4, "C"),
            {"stemp": "22.0", "mode": "modified_auto"},
        ),
        (lambda w: w.set_lock("dev1", NODE, ON), {"locked": True}),
        (lambda w: w.set_boost("dev1", NODE, ON), {"boost": True}),
    ],
)
async def test_status_write_round_trip(fake, write, expected_status_keys):
    await write(fake.writes)
    status = await fake.get_node_status("dev1", NODE)
    for key, value in expected_status_keys.items():
        assert status[key] == value
    # single-key semantics: untouched keys survive
    assert status["units"] == "C"
    assert status["mtemp"] == "190"


async def test_prog_temps_write_round_trip(fake):
    await fake.writes.set_prog_temps(
        "dev1",
        NODE,
        ice_temp=10.4,
        eco_temp=None,
        comf_temp=21.6,
        units="C",
    )
    status = await fake.get_node_status("dev1", NODE)
    assert status["ice_temp"] == "10.0"
    assert status["comf_temp"] == "21.5"
    assert "eco_temp" not in status


async def test_setup_write_mutates_setup_dict_and_keeps_other_keys(fake):
    await fake.writes.set_priority("dev1", NODE, "high")
    setup = await fake.get_node_setup("dev1", NODE)
    assert setup["priority"] == "high"
    assert setup["htr_brand"] == "acme"


async def test_setup_get_merge_post_posts_complete_object(fake):
    await fake.writes.set_window_mode("dev1", NODE, ON)
    method, path, body = fake.requests[-1]
    assert (method, path) == ("POST", "devs/dev1/htr/2/setup")
    assert body == {
        "priority": "low",
        "htr_brand": "acme",
        "extra_options": {"existing": 1},
        "window_mode_enabled": True,
    }


async def test_extra_options_round_trip(fake):
    await fake.writes.set_extra_options("dev1", NODE, {"changed": 1})
    setup = await fake.get_node_setup("dev1", NODE)
    assert setup["extra_options"]["existing"] == 1
    assert setup["extra_options"]["changed"] == 1


async def test_prog_write_merges_day_level(fake):
    new_day = [7] * 24
    await fake.writes.set_prog("dev1", NODE, {"1": new_day})
    payload = await fake.get_node_prog("dev1", NODE)
    assert payload["prog"] == {"0": SLOTS, "1": new_day, "6": SLOTS}


async def test_away_write_round_trip(fake):
    await fake.writes.set_away("dev1", ON)
    assert await fake.get_away_status("dev1") == {
        "away": True,
        "enabled": True,
        "forced": False,
    }


async def test_power_limit_round_trip(fake):
    box = FakeSmartbox()
    box.add_device("dev1", power_limit=2000)
    await box.writes.set_power_limit("dev1", 3000)
    assert await box.get_power_limit("dev1") == {"power_limit": 3000}


# --- requests log / failure scripting ---


async def test_requests_log_records_method_path_body(fake):
    await fake.writes.set_setpoint("dev1", NODE, 21.0, "C")
    await fake.get_node_status("dev1", NODE)
    assert (
        "POST",
        "devs/dev1/htr/2/status",
        {"stemp": "21.0", "units": "C"},
    ) in fake.requests
    assert ("GET", "devs/dev1/htr/2/status", None) in fake.requests


async def test_fail_next_4xx_is_one_shot(fake):
    fake.fail_next(404)
    with pytest.raises(SmartboxError, match="404"):
        await fake.get_node_status("dev1", NODE)
    assert await fake.get_connected("dev1") == {"connected": True}  # recovered


async def test_fail_next_5xx_single_attempt_state_unchanged(fake):
    fake.fail_next(500)
    with pytest.raises(TransportUnavailableError, match="500"):
        await fake.writes.set_setpoint("dev1", NODE, 21.0, "C")
    assert len([r for r in fake.requests if r[0] == "POST"]) == 1  # single attempt
    assert (await fake.get_node_status("dev1", NODE))["stemp"] == "20.0"  # untouched


async def test_fail_next_applies_to_writes(fake):
    fake.fail_next(409)
    with pytest.raises(SmartboxError, match="409"):
        await fake.writes.set_mode("dev1", NODE, "auto")
    assert (await fake.get_node_status("dev1", NODE))["mode"] == "auto"


async def test_unmodelled_post_path_raises(fake):
    with pytest.raises(SmartboxError, match="does not model"):
        await fake.post("devs/dev1/unknown/endpoint", {"x": 1})


async def test_write_to_unknown_node_raises(fake):
    with pytest.raises(SmartboxError, match="no node"):
        await fake.writes.set_mode("dev1", NodeRef("htr", "99"), "auto")


async def test_validation_fails_before_any_request(fake):
    with pytest.raises(ValueError, match="mode"):
        await fake.writes.set_mode("dev1", NODE, "turbo")
    assert fake.requests == []


# --- composition with FakeTransport: ONE consistent world ---


async def test_snapshot_through_transport_shows_written_value(fake):
    transport = FakeTransport()
    seen: list = []
    transport.subscribe(_collector(seen))
    await transport.deliver_snapshot(fake.snapshot_payload())
    await fake.writes.set_setpoint("dev1", NODE, 21.3, "C")
    await transport.deliver_snapshot(fake.snapshot_payload())
    first_status = seen[0].payload[0]["devs"][0]["devData"]["nodes"][0]["status"]
    second_status = seen[1].payload[0]["devs"][0]["devData"]["nodes"][0]["status"]
    assert first_status["stemp"] == "20.0"
    assert second_status["stemp"] == "21.0"  # the fake and the ws world agree


def _collector(seen: list):
    async def collect(event) -> None:
        seen.append(event)

    return collect


async def test_node_power_limit_round_trip(fake):
    """The node's own limit lands in its setup, as the live pmo capture shows."""
    fake.add_node("dev1", NodeRef("pmo", "0"), status={}, setup={"power_limit": 1500})
    await fake.writes.set_node_power_limit("dev1", NodeRef("pmo", "0"), 2000)
    setup = await fake.get_node_setup("dev1", NodeRef("pmo", "0"))
    assert setup["power_limit"] == 2000

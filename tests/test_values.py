"""Tests for boundary normalization (``smartbox_protocol.values``)."""

from __future__ import annotations

import pytest

from custom_components.smartbox.smartbox_protocol.errors import SmartboxError
from custom_components.smartbox.smartbox_protocol.values import (
    celsius_to_node_units,
    format_setpoint,
    is_lost,
    node_units_to_celsius,
    normalize_prog,
    parse_number,
)

# --- parse_number ---


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (20, 20.0),
        (0, 0.0),
        (-3.5, -3.5),
        (20.5, 20.5),
        ("20.0", 20.0),
        ("0.0", 0.0),
        ("0", 0.0),
        ("-5.5", -5.5),
        (" 21.5 ", 21.5),
    ],
)
def test_parse_number_accepts_numbers_and_numeric_strings(wire, expected):
    assert parse_number(wire) == expected


@pytest.mark.parametrize(
    "wire",
    [
        True,
        False,
        None,
        "abc",
        "",
        [],
        {},
        ["20"],
        20.0 + 0j,
        # non-finite spellings Python's float() would otherwise accept
        float("nan"),
        float("inf"),
        "nan",
        "inf",
        "Infinity",
        "1e400",
    ],
)
def test_parse_number_rejects_malformed(wire):
    with pytest.raises(SmartboxError, match="number"):
        parse_number(wire)


# --- format_setpoint ---


@pytest.mark.parametrize(
    ("temp", "expected"),
    [
        (21.3, "21.0"),  # device silently quantizes off-grid values; floor to the grid
        (22.4, "22.0"),
        (22.8, "22.5"),
        (21.0, "21.0"),
        (20.5, "20.5"),
        (0.4, "0.0"),
        (10.6, "10.5"),
        (21.25, "21.0"),  # half-grid edge: exactly between 21.0 and 21.5, floor wins
    ],
)
def test_format_setpoint_celsius_quantizes_to_half_grid(temp, expected):
    assert format_setpoint(temp, "C") == expected


def test_format_setpoint_fahrenheit_quantizes_to_whole_degrees():
    """The F grid is integral, and the body keeps the app's one decimal.

    A whole-degree F body without the decimal is rejected with a 400
    (live 2026-10-18), while the app posts ``.toFixed(1)`` values.
    """
    assert format_setpoint(72.3, "F") == "72.0"
    assert format_setpoint(72.5, "F") == "73.0"  # half up
    assert format_setpoint(72.75, "F") == "73.0"
    assert format_setpoint(72.0, "F") == "72.0"
    assert format_setpoint(-3.4, "F") == "-3.0"


def test_format_setpoint_rejects_unknown_units():
    with pytest.raises(ValueError, match="units"):
        format_setpoint(21.0, "K")


@pytest.mark.parametrize("units", ["C", "F"])
@pytest.mark.parametrize(
    "temp",
    ["abc", None, True, False, float("nan"), float("inf"), float("-inf"), [21.0]],
)
def test_format_setpoint_validates_the_temperature_value(units, temp):
    with pytest.raises(ValueError, match="temp"):
        format_setpoint(temp, units)


# --- is_lost ---


def test_is_lost_bare_lost_payload():
    assert is_lost({"sync_status": "lost"})


def test_is_lost_marked_full_payload():
    assert is_lost({"sync_status": "lost", "stemp": "20.0", "units": "C"})


@pytest.mark.parametrize(
    "payload",
    [
        {"sync_status": "ok", "stemp": "20.0"},
        {"stemp": "20.0"},  # no sync_status key
        {},
    ],
)
def test_is_lost_false(payload):
    assert not is_lost(payload)


# --- normalize_prog (the schedule-shape normalizer) ---


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # REST shape: nested under "prog", wrapper keys dropped
        ({"prog": {"0": [1, 2]}, "sync_status": "ok"}, {"0": [1, 2]}),
        # bare day-keyed object passes through
        ({"0": [1, 2], "6": [3]}, {"0": [1, 2], "6": [3]}),
        # non-dict payloads -> None
        ("garbage", None),
        (["0", [1]], None),
        (None, None),
        # non-dict "prog" -> None
        ({"prog": "garbage"}, None),
        # day keys must be strings
        ({0: [1, 2]}, None),
        # values must be lists of ints
        ({"0": "nope"}, None),
        ({"0": [1, "2"]}, None),
        ({"0": [1.5]}, None),
        # a tuple value is not a wire list
        ({"0": (1, 2)}, None),
    ],
)
def test_normalize_prog_shapes(payload, expected):
    assert normalize_prog(payload) == expected


def test_normalize_prog_copies_slot_lists():
    slots = [1, 2]
    prog = normalize_prog({"0": slots})
    assert prog is not None
    prog["0"].append(3)
    assert slots == [1, 2]


def test_celsius_to_node_units_snaps_fahrenheit_to_whole_degrees():
    """The app shows a stored 24.0 °C as 75.0 °F (decoded 2026-10-18)."""
    assert celsius_to_node_units(24.0, "C") == 24.0
    assert celsius_to_node_units(24.0, "F") == 75.0
    assert celsius_to_node_units(30.0, "F") == 86.0
    assert celsius_to_node_units(16.0, "F") == 61.0


def test_node_units_to_celsius_snaps_fahrenheit_to_the_half_grid():
    """A Celsius input passes through (format_setpoint floors it); F snaps."""
    assert node_units_to_celsius(22.4, "C") == 22.4
    assert node_units_to_celsius(75.0, "F") == 24.0
    assert node_units_to_celsius(86.0, "F") == 30.0
    assert node_units_to_celsius(70.0, "F") == 21.0

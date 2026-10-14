"""Payload-bounding guards.

Server-controlled data never lands verbatim in log lines or
exception messages.
"""

from __future__ import annotations

import logging

import pytest

from custom_components.smartbox.identity import async_discover_identity
from custom_components.smartbox.mapping import get_target_temperature
from custom_components.smartbox.smartbox_core import merging as merging_mod
from custom_components.smartbox.smartbox_protocol.errors import SmartboxError
from custom_components.smartbox.smartbox_protocol.values import parse_number

GIANT = "x" * 5000
# every bounded message must stay under this budget
BUDGET = 200


async def test_discover_identity_giant_payload_error_bounded():
    class Host:
        async def get_grouped_devices(self):
            return {"unexpected": GIANT}

    with pytest.raises(SmartboxError) as excinfo:
        await async_discover_identity(Host(), None)  # type: ignore[arg-type]
    assert len(str(excinfo.value)) < BUDGET
    assert GIANT not in str(excinfo.value)


async def test_discover_identity_giant_dev_entry_error_bounded():
    class Host:
        async def get_grouped_devices(self):
            return [{"id": "h1", "devs": [{"devData": {"name": GIANT}}]}]

    with pytest.raises(SmartboxError) as excinfo:
        await async_discover_identity(Host(), None)  # type: ignore[arg-type]
    assert len(str(excinfo.value)) < BUDGET
    assert GIANT not in str(excinfo.value)


async def test_discover_identity_giant_dev_key_error_bounded():
    # even a giant KEY NAME in the dev dict stays bounded
    class Host:
        async def get_grouped_devices(self):
            return [{"id": "h1", "devs": [{GIANT: "v"}]}]

    with pytest.raises(SmartboxError) as excinfo:
        await async_discover_identity(Host(), None)  # type: ignore[arg-type]
    assert len(str(excinfo.value)) < BUDGET
    assert GIANT not in str(excinfo.value)


def test_get_target_temperature_giant_status_error_bounded(caplog):
    with caplog.at_level(logging.DEBUG):
        # htr_mod path: missing selected_temp -> bounded KeyError
        with pytest.raises(KeyError) as excinfo:
            get_target_temperature("htr_mod", {"nope": GIANT})
        assert len(str(excinfo.value)) < BUDGET
        assert GIANT not in str(excinfo.value)
        # plain htr path: missing stemp -> bounded KeyError
        with pytest.raises(KeyError) as excinfo2:
            get_target_temperature("htr", {"no_stemp": GIANT})
        assert len(str(excinfo2.value)) < BUDGET
        assert GIANT not in str(excinfo2.value)
        # htr_mod path: a giant selected_temp VALUE is bounded too
        with pytest.raises(KeyError) as excinfo3:
            get_target_temperature("htr_mod", {"selected_temp": GIANT})
        assert len(str(excinfo3.value)) < BUDGET
        assert GIANT not in str(excinfo3.value)
    # no log record renders the payload (or a forged line)
    assert not [
        rec.getMessage() for rec in caplog.records if GIANT in rec.getMessage()
    ]


def test_parse_number_error_bounded_small_values_verbatim():
    with pytest.raises(SmartboxError) as excinfo:
        parse_number(GIANT)
    assert len(str(excinfo.value)) < BUDGET
    assert "x" * 100 not in str(excinfo.value)
    # small values still render verbatim
    with pytest.raises(SmartboxError, match=r"got: 'abc'"):
        parse_number("abc")
    with pytest.raises(SmartboxError, match="got bool: True"):
        truthy = True
        parse_number(truthy)


def test_malformed_node_entry_warning_carries_dev_id_not_payload(caplog):
    """The node-entry warning threads dev_id in; the payload stays out."""
    with caplog.at_level(logging.WARNING, logger=merging_mod.__name__):
        assert merging_mod.entry_key({GIANT: "v"}, "dev1") is None
    # Filter on the warning's own words rather than the key list it names
    # (that list changed when the wording was corrected to ``type``).
    msgs = [
        r.getMessage()
        for r in caplog.records
        if "Node entry missing" in r.getMessage()
    ]
    assert len(msgs) == 1
    assert "dev1" in msgs[0]
    assert GIANT not in msgs[0]

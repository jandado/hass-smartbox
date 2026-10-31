"""Every entity of the integration goes Unavailable when the feed dies.

The engine decides availability (per node from frames, feed-wide from silence
past the grace window) and the adapter renders it through the base entity's
``available``. This sweeps the RENDER side across every platform, so an entity
class that overrides ``available`` and forgets the feed - the connectivity
sensor does override it, because its whole point is to report "off" rather than
"unknown" - fails here instead of in the field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.smartbox.const import DOMAIN
from custom_components.smartbox.smartbox_protocol.events import SocketClosed
from tests.stack import push_status, seed_stack
from tests.test_utils import until

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from tests.mocks import MockSmartbox


def _integration_entities(hass: HomeAssistant) -> list:
    """Every entity the integration's platforms added to hass."""
    return [
        entity
        for component in hass.data["entity_components"].values()
        for entity in component.entities
        if entity.platform is not None and entity.platform.platform_name == DOMAIN
    ]


async def test_feed_outage_makes_every_entity_unavailable(
    hass: HomeAssistant, mock_smartbox: MockSmartbox, config_entry, recorder_mock
) -> None:
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await seed_stack(hass, mock_smartbox)

    entities = _integration_entities(hass)
    assert len(entities) > 20, "the sweep must cover every platform"
    available_before = {entity.entity_id for entity in entities if entity.available}
    assert len(available_before) > 20, "and must not be vacuous"

    runtime = config_entry.runtime_data
    runtime.engine.feed.grace_s = 0.05  # shrink the window for the test
    await runtime.engine.handle_event(SocketClosed(code=1006))

    await until(lambda: all(entity.available is False for entity in entities))
    # sanity: the state machine agrees with the property
    assert hass.states.get(next(iter(available_before))).state == "unavailable"

    # the first frame after the outage restores exactly what was up before
    mock_device = (await mock_smartbox.session.get_devices())[0]
    mock_node = (await mock_smartbox.session.get_nodes(mock_device["dev_id"]))[0]
    body = dict(
        await mock_smartbox.session.get_status(mock_device["dev_id"], mock_node)
    )
    body["mtemp"] = "19.1"
    await push_status(hass, runtime, mock_device["dev_id"], mock_node, body)

    await until(
        lambda: (
            {entity.entity_id for entity in entities if entity.available}
            == available_before
        )
    )

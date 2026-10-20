"""Tests for the integration's lifecycle.

Setup/probe outcome mapping, the full wiring smoke (real stack, offline
seams), the HA-stop teardown and the box-device creation plus the
per-node registry entry re-home.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
    translation,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.smartbox as smartbox_init
from custom_components.smartbox import async_setup_entry, update_listener
from custom_components.smartbox.adapter import ServiceTargets
from custom_components.smartbox.const import (
    CONF_DISPLAY_ENTITY_PICTURES,
    DOMAIN,
    FIELD_SCHEDULE,
    SERVICE_SET_SCHEDULE,
)
from custom_components.smartbox.history import HistoryImport
from custom_components.smartbox.mapping import remove_service_if_last
from custom_components.smartbox.smartbox_protocol import (
    AuthError,
    SmartboxClient,
    TransportUnavailableError,
    TransportUnsupportedError,
)
from tests.const import MOCK_SMARTBOX_CONFIG
from tests.fake_transport import FakeTransport
from tests.mocks import get_device_by_identifier

# Key -> entity domain of the per-node registry entries to re-home.
_REHOMED_TEST_KEYS = {
    "away_status": SWITCH_DOMAIN,
    "connected": BINARY_SENSOR_DOMAIN,
    "power_limit": NUMBER_DOMAIN,
}


@pytest.mark.asyncio
async def test_update_listener(hass, config_entry):
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await update_listener(hass, config_entry)
        mock_reload.assert_called_once_with(config_entry.entry_id)


@pytest.mark.asyncio
async def test_update_listener_ignores_a_history_only_change(hass, config_entry):
    """The first-setup history flag is written by the sensors, not the user.

    Reloading for it would tear down the websocket and cancel the imports of
    the nodes that have not finished, so the listener must ignore it - and still
    reload for any other option.
    """
    entry = SimpleNamespace(
        entry_id="entry_1",
        options={"history_consumption": "auto"},
        runtime_data=SimpleNamespace(
            history=HistoryImport(["node_1"], {"history_consumption": "start"})
        ),
    )
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await update_listener(hass, entry)
        mock_reload.assert_not_called()
        # any other option still reloads
        entry.options = {
            "history_consumption": "auto",
            CONF_DISPLAY_ENTITY_PICTURES: True,
        }
        await update_listener(hass, entry)
        mock_reload.assert_called_once_with("entry_1")


@pytest.mark.asyncio
async def test_update_listener_reloads_when_credentials_change(hass, config_entry):
    """A reauth data update rebuilds the client, so the listener reloads."""
    entry = SimpleNamespace(
        entry_id="entry_1",
        data={"api_name": "api", "username": "user@example.com", "password": "new"},
        options={},
        runtime_data=SimpleNamespace(
            history=HistoryImport([], {}),
            entry_data={
                "api_name": "api",
                "username": "user@example.com",
                "password": "old",
            },
        ),
    )
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await update_listener(hass, entry)
        mock_reload.assert_called_once_with("entry_1")


@pytest.mark.asyncio
async def test_update_listener_reloads_when_start_is_reselected(hass, config_entry):
    """Selecting ``start`` asks for the first-setup import again.

    The pending set is seeded when the runtime is built, so a reload is the
    only way the request can take effect.
    """
    entry = SimpleNamespace(
        entry_id="entry_1",
        options={"history_consumption": "start"},
        runtime_data=SimpleNamespace(
            history=HistoryImport(["node_1"], {"history_consumption": "auto"})
        ),
    )
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await update_listener(hass, entry)
        mock_reload.assert_called_once_with("entry_1")


# --- setup mapping (probe + identity discovery outcomes) ---


async def _setup_with_probe(hass, config_entry, probe):
    """Run async_setup_entry with the capability probe scripted."""
    with (
        patch(
            "custom_components.smartbox.check_ws_user_support",
            new=probe,
        ),
    ):
        return await async_setup_entry(hass, config_entry)


@pytest.mark.asyncio
async def test_setup_entry_probe_unsupported_raises_config_entry_error(
    hass, mock_smartbox, config_entry
):
    with pytest.raises(ConfigEntryError) as exc_info:
        await _setup_with_probe(
            hass,
            config_entry,
            AsyncMock(side_effect=TransportUnsupportedError("404")),
        )
    assert exc_info.value.translation_key == "ws_user_unsupported"
    # the rendered message carries the status text, no literal {error}
    hass.config.components.add(DOMAIN)  # the loader needs the component
    await translation.async_load_integrations(hass, {DOMAIN})
    message = translation.async_get_exception_message(
        DOMAIN,
        exc_info.value.translation_key,
        exc_info.value.translation_placeholders,
    )
    assert "404" in message
    assert "{error}" not in message
    assert config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.asyncio
async def test_setup_entry_probe_transient_raises_not_ready(
    hass, mock_smartbox, config_entry
):
    with pytest.raises(ConfigEntryNotReady):
        await _setup_with_probe(
            hass,
            config_entry,
            AsyncMock(side_effect=TransportUnavailableError("timeout")),
        )


@pytest.mark.asyncio
async def test_setup_entry_malformed_node_reply_retries(
    hass, mock_smartbox, config_entry
):
    """A ``[{}]`` node reply is a bounded retry, never a KeyError traceback.

    Subscripting the server's mapping directly let this reply escape
    ``async_setup_entry``'s error mapping; it is a ``SmartboxError``, i.e.
    ``ConfigEntryNotReady``.
    """
    with (
        patch.object(
            SmartboxClient,
            "get_nodes",
            new=AsyncMock(return_value=[{}]),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, config_entry)


@pytest.mark.asyncio
async def test_setup_entry_token_auth_error_raises_auth_failed(
    hass, mock_smartbox, config_entry
):
    """A rejected password grant at setup starts the reauth flow."""
    with (
        patch(
            "custom_components.smartbox.smartbox_protocol.auth.TokenManager.get_token",
            new=AsyncMock(side_effect=AuthError("rejected")),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, config_entry)


@pytest.mark.asyncio
async def test_setup_entry_token_transient_raises_not_ready(
    hass, mock_smartbox, config_entry
):
    """A transient token-grant failure retries via ConfigEntryNotReady."""
    with (
        patch(
            "custom_components.smartbox.smartbox_protocol.auth.TokenManager.get_token",
            new=AsyncMock(side_effect=TransportUnavailableError("timeout")),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, config_entry)


# --- full wiring smoke (real stack, offline seams) ---


@pytest.mark.asyncio
async def test_setup_entry_wiring_full_stack(hass, mock_smartbox, config_entry):
    """Setup wires engine+client+transport+auth and supervises them."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = config_entry.runtime_data
    assert runtime is not None
    engine = runtime.engine
    assert engine is not None
    # The transport is supervised (FakeTransport.run parks like the real
    # socket).
    assert runtime.auth is runtime.client.auth
    assert engine.supervision_task is not None
    assert not engine.supervision_task.done()
    supervision_task = engine.supervision_task

    # Identity was discovered over the (mocked) client reads: two devices,
    # all nodes carrying names/versions for the registry surface.
    assert [device.dev_id for device in runtime.identity.devices] == [
        "device_1",
        "device_2",
    ]
    assert len(runtime.identity.nodes) == 8
    htr_node = runtime.identity.nodes[0]
    assert htr_node.name == "Device 1 0"
    assert htr_node.pid == "product_id_1_0"

    # Unload stops supervision, closes the transport and clears the runtime.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    # stop() clears the engine's reference; the captured task is done.
    assert engine.supervision_task is None
    assert supervision_task.done()
    assert runtime.transport.closed
    # HA clears entry.runtime_data once the platforms unloaded.
    assert not hasattr(config_entry, "runtime_data")
    assert config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.asyncio
async def test_hass_stop_tears_down_transport(hass, mock_smartbox, config_entry):
    """The HA-stop listener tears the supervised transport down (bounded)."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = config_entry.runtime_data
    supervision_task = runtime.engine.supervision_task

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert runtime.transport.closed
    assert supervision_task.done()


@pytest.mark.asyncio
async def test_setup_failure_after_supervision_tears_the_transport_down(
    hass, mock_smartbox, config_entry
):
    """A setup that fails after the transport is live must stop it.

    HA runs the on-unload callbacks for a FAILED setup too, and the supervision
    task is not entry-tracked: without the teardown registered before the
    platform forward, the socket would stay open against a runtime nothing owns
    and the retry would open a second one.
    """
    boom_msg = "platform boom"
    boom = RuntimeError(boom_msg)
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(side_effect=boom),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    runtime = config_entry.runtime_data
    assert runtime.transport.closed
    assert runtime.engine.supervision_task is None


class _FlappingTransport(FakeTransport):
    """FakeTransport whose first ``run()`` raises ``TransportUnsupportedError``."""

    def __init__(self) -> None:
        super().__init__()
        self.runs = 0

    async def run(self) -> None:
        """Fail once (a backend rollback), then park like the real socket."""
        self.runs += 1
        if self.runs == 1:
            msg = "endpoint gone"
            raise TransportUnsupportedError(msg)
        await super().run()


@pytest.mark.asyncio
async def test_midrun_unsupported_recovery_keeps_the_entry_loaded(
    hass, mock_smartbox, config_entry, monkeypatch
):
    """A mid-run ws_user disappearance is recovered IN PLACE.

    The monitor re-supervises the restartable transport rather than reloading
    the entry: an entry-tracked reload task is cancelled by the reload's own
    unload, so the entry would be torn down and never set up again. The entry
    must therefore stay loaded, with its entities, and no reload may run.
    """
    monkeypatch.setattr(smartbox_init, "backoff_delay", lambda *_: 0.0)
    transport = _FlappingTransport()

    def _transport_stub(token_source, api_host, **kwargs) -> _FlappingTransport:
        """Stands in for WsUserTransport: the one scripted transport."""
        return transport

    with (
        patch("custom_components.smartbox.WsUserTransport", _transport_stub),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_,
        patch.object(
            hass.config_entries, "async_schedule_reload", AsyncMock()
        ) as schedule_,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        for _ in range(10):
            if transport.runs >= 2:
                break
            await hass.async_block_till_done()

    assert transport.runs >= 2  # the monitor re-probed in place
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.engine.supervision_task is not None
    assert hass.states.async_entity_ids(CLIMATE_DOMAIN)
    reload_.assert_not_called()
    schedule_.assert_not_called()


@pytest.mark.asyncio
async def test_unload_completes_when_transport_close_hangs(
    hass, mock_smartbox, config_entry, monkeypatch
):
    """A transport whose close() never returns must not stall unload.

    The teardown bounds the transport close with
    ``_WS_USER_TEARDOWN_TIMEOUT_SECONDS``; a hung close times out
    (suppressed) and the entry still unloads cleanly.
    """
    monkeypatch.setattr(
        "custom_components.smartbox._WS_USER_TEARDOWN_TIMEOUT_SECONDS", 0.05,
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = config_entry.runtime_data
    engine = runtime.engine
    supervision_task = engine.supervision_task

    hang = asyncio.Event()

    async def hanging_close():
        await hang.wait()

    with patch.object(runtime.transport, "close", side_effect=hanging_close):
        # the unload itself is bounded (this would hang forever otherwise)
        assert await asyncio.wait_for(
            hass.config_entries.async_unload(config_entry.entry_id), 5.0,
        )
    await hass.async_block_till_done()

    # engine.stop() completed: the supervision task was cancelled + cleared
    assert engine.supervision_task is None
    assert supervision_task.done()
    # the transport was NEVER really closed - the timeout path was taken
    assert not runtime.transport.closed
    # HA clears entry.runtime_data once the platforms unloaded.
    assert not hasattr(config_entry, "runtime_data")
    assert config_entry.state is ConfigEntryState.NOT_LOADED


# --- box device creation + per-node registry re-home ---


async def test_box_device_created_and_per_node_entries_rehomed(
    hass, mock_smartbox, config_entry
):
    """The box is its own device; per-node registry entries re-home.

    Entries created on the per-node devices carry the away switch,
    connectivity sensor and
    power limit with unique_ids
    ``{dev_id}_{addr}_{key}`` — including duplicates per node. On setup they
    must be re-keyed onto the box device (first/lowest addr per key) and the
    duplicates removed.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    # Pre-box-device registry state: per-node devices carrying the
    # device-level entries earlier releases created.
    for dev_id in ("device_1", "device_2"):
        node_device = device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(DOMAIN, f"{dev_id}_node0")},
        )
        for addr in (0, 1):
            for key in ("away_status", "connected"):
                entity_registry.async_get_or_create(
                    _REHOMED_TEST_KEYS[key],
                    DOMAIN,
                    f"{dev_id}_{addr}_{key}",
                    config_entry=config_entry,
                    device_id=node_device.id,
                    suggested_object_id=f"{dev_id}_{addr}_{key}",
                )
        entity_registry.async_get_or_create(
            NUMBER_DOMAIN,
            DOMAIN,
            f"{dev_id}_0_power_limit",
            config_entry=config_entry,
            device_id=node_device.id,
            suggested_object_id=f"{dev_id}_0_power_limit",
        )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    device_dicts = {
        device_dict["dev_id"]: device_dict
        for device_dict in await mock_smartbox.session.get_devices()
    }
    for dev_id in ("device_1", "device_2"):
        box_device = get_device_by_identifier(device_registry, (DOMAIN, dev_id))
        assert box_device is not None
        assert box_device.name == device_dicts[dev_id]["name"]
        assert box_device.sw_version == device_dicts[dev_id]["fw_version"]
        assert box_device.serial_number == device_dicts[dev_id]["serial_id"]
        for key, domain in _REHOMED_TEST_KEYS.items():
            entity_id = entity_registry.async_get_entity_id(
                domain, DOMAIN, f"{dev_id}_{key}"
            )
            assert entity_id is not None, f"{dev_id}_{key} not re-keyed"
            entity_entry = entity_registry.async_get(entity_id)
            assert entity_entry.device_id == box_device.id
        # Duplicates beyond the first addr were removed.
        assert (
            entity_registry.async_get_entity_id(
                SWITCH_DOMAIN, DOMAIN, f"{dev_id}_1_away_status"
            )
            is None
        )
        assert (
            entity_registry.async_get_entity_id(
                BINARY_SENSOR_DOMAIN, DOMAIN, f"{dev_id}_1_connected"
            )
            is None
        )

    # Re-running setup must be a no-op (idempotent re-home).
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        entity_registry.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, "device_1_away_status")
        is not None
    )
    assert (
        entity_registry.async_get_entity_id(
            SWITCH_DOMAIN, DOMAIN, "device_1_0_away_status"
        )
        is None
    )


# --- ws_user repair-issue lifecycle ---


@pytest.mark.asyncio
async def test_flapping_issue_registered_and_pruned_on_remove(hass, config_entry):
    """The repair issue is created and pruned by entry removal."""
    smartbox_init._create_flapping_issue(
        hass, config_entry, TransportUnsupportedError("404")
    )
    issue_id = (DOMAIN, smartbox_init.ws_user_flapping_issue_id(config_entry))
    assert issue_id in ir.async_get(hass).issues

    await smartbox_init.async_remove_entry(hass, config_entry)

    assert issue_id not in ir.async_get(hass).issues


@pytest.mark.asyncio
async def test_successful_setup_clears_a_leftover_repair_issue(
    hass, mock_smartbox, config_entry
):
    """A successful load means the endpoint serves: the issue is cleared."""
    issue_id = (DOMAIN, smartbox_init.ws_user_flapping_issue_id(config_entry))
    smartbox_init._create_flapping_issue(
        hass, config_entry, TransportUnsupportedError("404")
    )
    assert issue_id in ir.async_get(hass).issues

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert issue_id not in ir.async_get(hass).issues

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_set_schedule_reaches_every_loaded_entry(
    hass, mock_smartbox, config_entry
):
    """A domain-global service call fans out across loaded entries.

    The handler is registered once (by whichever entry loads first) and must
    reach the entities of every loaded entry, not only the registrant's.
    """
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    calls: list[dict] = []

    class _OtherSchedule:
        entity_id = "sensor.other_entry_schedule"

        async def async_set_schedule(self, prog) -> None:
            calls.append(prog)

    other = MockConfigEntry(domain=DOMAIN, data=dict(MOCK_SMARTBOX_CONFIG[DOMAIN]))
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.LOADED)
    other.runtime_data = SimpleNamespace(
        services=ServiceTargets(schedule=[_OtherSchedule()])
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SCHEDULE,
        {
            FIELD_SCHEDULE: {"0": [1] * 24},
            "entity_id": "sensor.other_entry_schedule",
        },
        blocking=True,
    )
    assert calls == [{"0": [1] * 24}]


async def test_unload_keeps_the_service_while_another_entry_is_loaded(
    hass, mock_smartbox, config_entry
):
    """Unloading one entry must not take the service away from the others."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    other = MockConfigEntry(domain=DOMAIN, data=dict(MOCK_SMARTBOX_CONFIG[DOMAIN]))
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.LOADED)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE)

    other.mock_state(hass, ConfigEntryState.NOT_LOADED)
    remove_service_if_last(hass, SERVICE_SET_SCHEDULE)
    assert not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE)

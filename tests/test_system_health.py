"""Tests for the integration's system health page."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartbox.smartbox_protocol.client import SmartboxClient
from custom_components.smartbox.smartbox_protocol.errors import SmartboxError
from custom_components.smartbox.system_health import async_register, system_health_info
from tests.const import DOMAIN, MOCK_SMARTBOX_CONFIG


async def test_system_health_no_entries(hass):
    """Without a config entry there is nothing to report."""
    assert await system_health_info(hass) == {}


async def test_system_health_api_facts(hass, mock_smartbox, config_entry):
    """API version/health read over the entry's client; reseller URL checked."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with (
        patch.object(
            SmartboxClient,
            "get_api_version",
            new=AsyncMock(
                return_value={"major": 1, "minor": 2, "subminor": 3, "commit": "abc"}
            ),
        ),
        patch.object(
            SmartboxClient,
            "health_check",
            new=AsyncMock(return_value={"message": "All good"}),
        ),
        patch(
            "homeassistant.components.system_health.async_check_can_reach_url",
            new=AsyncMock(return_value="reachable"),
        ) as reach,
    ):
        info = await system_health_info(hass)

    assert info["api_version"] == "1.2.3.abc"
    assert info["api_health_check"] == "All good"
    # System health awaits the reachability coroutine itself.
    assert await info["can_reach_web_reseller"] == "reachable"
    runtime = config_entry.runtime_data
    assert reach.await_args.args[1] == runtime.identity.devices[0].reseller.web_url


async def test_system_health_maps_malformed_reads(hass, mock_smartbox, config_entry):
    """A malformed success is reported as an error, not raised."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with (
        patch.object(
            SmartboxClient,
            "get_api_version",
            new=AsyncMock(side_effect=SmartboxError("bad")),
        ),
        patch.object(
            SmartboxClient,
            "health_check",
            new=AsyncMock(side_effect=KeyError("message")),
        ),
        patch(
            "homeassistant.components.system_health.async_check_can_reach_url",
            new=AsyncMock(return_value="reachable"),
        ),
    ):
        info = await system_health_info(hass)

    assert info["api_version"] == "error: SmartboxError"
    assert info["api_health_check"] == "error: KeyError"
    assert await info["can_reach_web_reseller"] == "reachable"


async def test_system_health_registers_its_callback(hass):
    """The system-health registration points at the info callback."""
    registration = MagicMock()
    async_register(hass, registration)
    registration.async_register_info.assert_called_once_with(system_health_info)


async def test_system_health_failing_entry(hass, mock_smartbox, config_entry):
    """A failing/retrying entry reports unknown facts instead of crashing."""
    info = await system_health_info(hass)
    assert info == {
        "api_version": None,
        "api_health_check": None,
        "can_reach_web_reseller": None,
    }


def _loaded_entry(hass, title: str, runtime) -> MockConfigEntry:
    """Add a LOADED mock entry carrying a fake runtime."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=f"{DOMAIN}_{title}",
        data=dict(MOCK_SMARTBOX_CONFIG[DOMAIN]),
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = runtime
    return entry


def _fake_runtime(url: str, version: str, health: str) -> SimpleNamespace:
    """Build the runtime surface system health reads."""
    major, minor, subminor, commit = version.split(".")
    return SimpleNamespace(
        client=SimpleNamespace(
            get_api_version=AsyncMock(
                return_value={
                    "major": int(major),
                    "minor": int(minor),
                    "subminor": int(subminor),
                    "commit": commit,
                }
            ),
            health_check=AsyncMock(return_value={"message": health}),
        ),
        identity=SimpleNamespace(
            devices=[
                SimpleNamespace(reseller=SimpleNamespace(name="Haverland", web_url=url))
            ]
        ),
        # The entry is LOADED, so pytest's teardown unloads it.
        membership=SimpleNamespace(
            async_pause=AsyncMock(),
            resume=MagicMock(),
            close=MagicMock(),
        ),
        engine=SimpleNamespace(stop=AsyncMock()),
        transport=SimpleNamespace(close=AsyncMock()),
    )


async def test_system_health_reports_every_loaded_entry(hass):
    """Two accounts produce one joined value per fact, plus the account count."""
    _loaded_entry(
        hass,
        "Haverland home@example.com",
        _fake_runtime("https://home/", "1.2.3.abc", "ok"),
    )
    _loaded_entry(
        hass,
        "Haverland cabin@example.com",
        _fake_runtime("https://cabin/", "4.5.6.def", "fine"),
    )

    with patch(
        "homeassistant.components.system_health.async_check_can_reach_url",
        new=AsyncMock(return_value="ok"),
    ) as reach:
        info = await system_health_info(hass)
        reachable = await info["can_reach_web_reseller"]

    assert info["accounts"] == 2
    assert info["unavailable_accounts"] == 0
    assert info["api_version"] == "Haverland #1: 1.2.3.abc; Haverland #2: 4.5.6.def"
    assert info["api_health_check"] == "Haverland #1: ok; Haverland #2: fine"
    assert "home@example.com" not in str(info)
    assert "cabin@example.com" not in str(info)
    assert reachable == "ok"
    assert sorted(call.args[1] for call in reach.await_args_list) == [
        "https://cabin/",
        "https://home/",
    ]


async def test_system_health_reports_a_failed_portal(hass):
    """One unreachable portal fails the aggregate without hiding the other."""
    _loaded_entry(hass, "Home", _fake_runtime("https://home/", "1.2.3.abc", "ok"))
    _loaded_entry(hass, "Cabin", _fake_runtime("https://cabin/", "4.5.6.def", "ok"))

    with patch(
        "homeassistant.components.system_health.async_check_can_reach_url",
        new=AsyncMock(side_effect=["ok", {"type": "failed", "error": "unreachable"}]),
    ):
        info = await system_health_info(hass)
        reachable = await info["can_reach_web_reseller"]

    assert reachable == "failed: 1"


async def test_system_health_counts_an_entry_without_a_runtime(hass):
    """A loaded but failing entry is counted, while its facts stay omitted."""
    _loaded_entry(hass, "Home", _fake_runtime("https://home/", "1.2.3.abc", "ok"))
    _loaded_entry(hass, "Cabin", None)

    with patch(
        "homeassistant.components.system_health.async_check_can_reach_url",
        new=AsyncMock(return_value="ok"),
    ):
        info = await system_health_info(hass)
        reachable = await info["can_reach_web_reseller"]

    assert info["accounts"] == 2
    assert info["unavailable_accounts"] == 1
    assert info["api_version"] == "Haverland #1: 1.2.3.abc"
    assert info["api_health_check"] == "Haverland #1: ok"
    assert reachable == "ok"


async def test_system_health_counts_a_setup_retry_entry(hass):
    """A configured-but-failing account is counted as unavailable."""
    _loaded_entry(hass, "Home", _fake_runtime("https://home/", "1.2.3.abc", "ok"))
    failing = MockConfigEntry(
        domain=DOMAIN,
        title="Cabin",
        unique_id="smartbox_Cabin",
        data=dict(MOCK_SMARTBOX_CONFIG[DOMAIN]),
    )
    failing.add_to_hass(hass)
    failing.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    with patch(
        "homeassistant.components.system_health.async_check_can_reach_url",
        new=AsyncMock(return_value="ok"),
    ):
        info = await system_health_info(hass)
        reachable = await info["can_reach_web_reseller"]

    assert info["accounts"] == 2
    assert info["unavailable_accounts"] == 1
    assert info["api_version"] == "Haverland #1: 1.2.3.abc"
    assert reachable == "ok"


async def test_system_health_reports_all_failing_accounts(hass):
    """Every configured account is counted even when none has a runtime."""
    for title in ("Home", "Cabin"):
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=title,
            unique_id=f"smartbox_{title}",
            data=dict(MOCK_SMARTBOX_CONFIG[DOMAIN]),
        )
        entry.add_to_hass(hass)
        entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    info = await system_health_info(hass)
    assert info == {
        "accounts": 2,
        "unavailable_accounts": 2,
        "api_version": None,
        "api_health_check": None,
        "can_reach_web_reseller": None,
    }

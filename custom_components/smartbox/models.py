"""Models for Smartbox."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any, Final, cast

from homeassistant.components.climate import (
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    HVACMode,
)
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    UnitOfTemperature,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from smartbox import AsyncSmartboxSession, SmartboxNodeType, UpdateManager
from smartbox.error import APIUnavailableError, InvalidAuthError, SmartboxError

from .const import (
    DEFAULT_BOOST_TEMP,
    DEFAULT_BOOST_TIME,
    DOMAIN,
    GITHUB_ISSUES_URL,
    HEATER_NODE_TYPES,
    PRESET_FROST,
    PRESET_SCHEDULE,
    PRESET_SELF_LEARN,
    BoostConfig,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Upper bound for websocket teardown (cancel + watchdog exit). Unload/reload
# and the HA-stop hook must always complete promptly even if the websocket
# session is wedged.
_TEARDOWN_TIMEOUT_SECONDS: Final = 10.0

# Backoff for restarting the update-manager task after an unexpected exit
# (run() itself waits out transient trouble; this only bounds how fast we
# retry when it died of something unexpected).
_WATCHDOG_RESTART_BASE_SECONDS: Final = 5.0
_WATCHDOG_RESTART_MAX_SECONDS: Final = 300.0
# A programme day always covers 24 h: slot length = 1440 / len(day array).
_PROG_DAY_MINUTES: Final = 1440

FactoryOptionsDict = dict[str, bool]
SetupDict = dict[str, Any]
StatusDict = dict[str, Any]
SamplesDict = list[dict[str, Any]]
ProgDict = dict[str, list[int]]
Node = dict[str, Any]
Device = dict[str, Any]


class SmartboxDevice:
    """Smartbox device."""

    def __init__(
        self,
        device: Device,
        session: AsyncSmartboxSession,
        hass: HomeAssistant,
    ) -> None:
        """Initialise a smartbox device."""
        self._device = device
        self._session = session
        self._away: bool = False
        self._power_limit: int = 0
        self._nodes: dict[tuple[str, int | str], SmartboxNode] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._hass = hass
        self._connected_status: bool | None = None
        self._watchdog_restarts: int = 0
        self._stopping: bool = False
        self.update_manager: UpdateManager = UpdateManager(
            self._session,
            self.dev_id,
        )

    @classmethod
    async def initialise_nodes(
        cls,
        device: Device,
        session: AsyncSmartboxSession,
        hass: HomeAssistant,
    ) -> SmartboxDevice:
        """Initilaise nodes."""
        self = cls(device=device, session=session, hass=hass)
        # Would do in __init__, but needs to be a coroutine
        self._connected_status = cast(
            "dict[str, bool]", (await self._session.get_device_connected(self.dev_id))
        )["connected"]
        self._away = cast(
            "dict[str, bool]", (await self._session.get_device_away_status(self.dev_id))
        )["away"]

        session_nodes = cast("list[Node]", await self._session.get_nodes(self.dev_id))
        if any(n.get("type") == SmartboxNodeType.PMO for n in session_nodes):
            self._power_limit = await self._session.get_device_power_limit(self.dev_id)

        for node_info in session_nodes:
            node: SmartboxNode = await SmartboxNode.create(
                device=self, node_info=node_info, session=self._session
            )

            self._nodes[(node.node_type, node.addr)] = node
        _LOGGER.debug("Creating SocketSession for device %s", self.dev_id)
        self.update_manager.subscribe_to_device_connected(self._connected)
        self.update_manager.subscribe_to_device_away_status(self._away_status_update)
        self.update_manager.subscribe_to_node_setup(self._node_setup_update)
        self.update_manager.subscribe_to_device_power_limit(self._power_limit_update)
        self.update_manager.subscribe_to_node_status(self._node_status_update)
        self.update_manager.subscribe_to_node_prog(self._node_prog_update)

        _LOGGER.debug("Starting UpdateManager task for device %s", self.dev_id)
        self._watchdog_task = asyncio.create_task(
            self.update_manager.run(), name=f"smartbox_update_{self.dev_id}"
        )
        # run() exiting unexpectedly (e.g. credentials rejected) must never
        # leave a silently dead socket: log it, then restart or trigger reauth.
        self._watchdog_task.add_done_callback(self._watchdog_done)
        return self

    async def cancel(self) -> None:
        """Cancel the watchdog task and disconnect.

        self._stopping = True
        Deliberately bounded and exception-proof: unload (UI reload) and the
        HA-stop hook must always complete promptly even when the websocket
        session is wedged. On live hardware run() parks in the websocket
        loop, so the watchdog task is still alive here; hard-cancelling it
        and then awaiting it bare used to raise CancelledError out of
        async_unload_entry, which silently killed UI reloads.
        """
        try:
            await asyncio.wait_for(
                self.update_manager.cancel(), _TEARDOWN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            _LOGGER.warning(
                "Socket teardown for device %s did not finish in %ss",
                self.dev_id,
                _TEARDOWN_TIMEOUT_SECONDS,
            )
        except Exception:
            _LOGGER.exception(
                "Error tearing down socket session for device %s", self.dev_id
            )
        finally:
            task = self._watchdog_task
            if task is not None:
                if not task.done():
                    _LOGGER.warning(
                        "Force-cancelling watchdog task for device %s", self.dev_id
                    )
                    task.cancel()
                # asyncio.wait returns on timeout instead of raising, and it
                # swallows the cancelled task's CancelledError for us; the
                # suppress only guards our own cancellation mid-wait.
                with suppress(asyncio.CancelledError):
                    await asyncio.wait({task}, timeout=_TEARDOWN_TIMEOUT_SECONDS)
                if not task.done():
                    _LOGGER.warning(
                        "Watchdog task for device %s still running after cancel",
                        self.dev_id,
                    )

    def _connected(self, connected: bool) -> None:
        _LOGGER.debug("Connected connected update: %s", connected)
        self._connected_status = connected
        if connected:
            # A (re)connection succeeded: any restart backoff can reset.
            self._watchdog_restarts = 0
        # Node-level entities listen on f"{DOMAIN}_{node.node_id}_connected";
        # dispatch per node so they actually receive connectivity updates.
        for node in self._nodes.values():
            async_dispatcher_send(
                self._hass,
                f"{DOMAIN}_{node.node_id}_connected",
                self._connected_status,
            )

    def _away_status_update(self, away_status: dict[str, bool]) -> None:
        _LOGGER.debug("Away status update: %s", away_status)

        if self._away != away_status["away"]:
            self._away = away_status["away"]
            for node in self._nodes.values():
                async_dispatcher_send(
                    self._hass, f"{DOMAIN}_{node.node_id}_away_status", self._away
                )

    def _power_limit_update(self, power_limit: int) -> None:
        _LOGGER.debug("power_limit update: %s", power_limit)
        if self._power_limit != power_limit:
            self._power_limit = power_limit
            async_dispatcher_send(
                self._hass, f"{DOMAIN}_{self.dev_id}_power_limit", power_limit
            )

    def _node_status_update(
        self, node_type: str, addr: int, node_status: StatusDict
    ) -> None:
        if node_type == SmartboxNodeType.PMO:
            return
        _LOGGER.debug("Node status update: %s", node_status)
        if node_status is not None and (node_type, addr) in self._nodes:
            node: SmartboxNode | None = self._nodes.get((node_type, addr), None)
            if node is not None and node.status != node_status:
                node.update_status(node_status)
                async_dispatcher_send(
                    self._hass, f"{DOMAIN}_{node.node_id}_status", node_status
                )
        else:
            _LOGGER.error(
                "Received status update for unknown node %s %s", node_type, addr
            )

    def _node_setup_update(
        self, node_type: str, addr: int, node_setup: SetupDict
    ) -> None:
        _LOGGER.debug("Node setup update: %s", node_setup)
        if (node_type, addr) in self._nodes:
            node: SmartboxNode | None = self._nodes.get((node_type, addr), None)
            if node is not None and node.setup != node_setup:
                node.update_setup(node_setup)
                async_dispatcher_send(
                    self._hass, f"{DOMAIN}_{node.node_id}_setup", node_setup
                )
        else:
            _LOGGER.error(
                "Received setup update for unknown node %s %s", node_type, addr
            )

    def _node_prog_update(self, node_type: str, addr: int, payload: Any) -> None:  # noqa: ANN401
        """Node prog (schedule) update from the websocket.

        The /prog update frame shape is recorded in ../smartbox api-notes.md
        but not yet live-confirmed; anything unexpected is ignored so the REST
        poll remains the authoritative source for the schedule.
        """
        if node_type == SmartboxNodeType.PMO:
            return
        prog = _normalize_prog(payload)
        if prog is None:
            _LOGGER.debug(
                "Ignoring unexpected prog payload for %s %s: %s",
                node_type,
                addr,
                payload,
            )
            return
        if (node_type, addr) in self._nodes:
            node: SmartboxNode | None = self._nodes.get((node_type, addr), None)
            if node is not None and node.prog != prog:
                node.update_prog(prog)
                async_dispatcher_send(
                    self._hass, f"{DOMAIN}_{node.node_id}_prog", prog
                )
        else:
            _LOGGER.error(
                "Received prog update for unknown node %s %s", node_type, addr
            )

    def _watchdog_done(self, task: asyncio.Task) -> None:
        """Handle the update-manager task exiting.

        Cancellation (unload, HA stop) is the normal exit. InvalidAuthError
        means the credentials were rejected after the library's automatic
        re-auth-and-resend also failed: request a reauthentication flow.
        Anything else is unexpected -- log it and restart the task with capped
        backoff instead of leaving a dead socket and stale entities behind.
        """
        if self._stopping or task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, InvalidAuthError):
            _LOGGER.error(
                "Credentials rejected for device %s; requesting reauthentication",
                self.dev_id,
            )
            async_dispatcher_send(self._hass, f"{DOMAIN}_{self.dev_id}_reauth_required")
            return
        if exc is None:
            _LOGGER.error(
                "Update task for device %s exited unexpectedly; restarting",
                self.dev_id,
            )
        else:
            _LOGGER.error(
                "Update task for device %s exited unexpectedly: %s; restarting",
                self.dev_id,
                exc,
                exc_info=exc,
            )
        self._schedule_watchdog_restart()

    def _schedule_watchdog_restart(self) -> None:
        """Restart the update-manager task after a capped, doubling backoff."""
        delay = min(
            _WATCHDOG_RESTART_BASE_SECONDS * (2**self._watchdog_restarts),
            _WATCHDOG_RESTART_MAX_SECONDS,
        )
        self._watchdog_restarts += 1

        async def _restart() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            _LOGGER.debug("Restarting update task for device %s", self.dev_id)
            self._watchdog_task = asyncio.create_task(
                self.update_manager.run(), name=f"smartbox_update_{self.dev_id}"
            )
            self._watchdog_task.add_done_callback(self._watchdog_done)

        self._hass.async_create_task(
            _restart(), name=f"smartbox_update_restart_{self.dev_id}"
        )

    @property
    def device(self) -> Device:
        """Return the device."""
        return self._device

    @property
    def connected(self) -> bool | None:
        """Return the device."""
        return self._connected_status

    @property
    def home(self) -> dict[str, Any]:
        """Return home of the device."""
        return self._device["home"]

    @property
    def dev_id(self) -> str:
        """Return the device id."""
        return self._device["dev_id"]

    def get_nodes(self) -> list[SmartboxNode]:
        """Return all nodes."""
        for item in self._nodes:
            _LOGGER.debug("Get_nodes: %s", item)
        return list(self._nodes.values())

    @property
    def name(self) -> str:
        """Return name of the device."""
        return self._device["name"]

    @property
    def model_id(self) -> int:
        """Return the model id."""
        return self._device["product_id"]

    @property
    def sw_version(self) -> int:
        """Return the software version of the device."""
        return self._device["fw_version"]

    @property
    def serial_number(self) -> int:
        """Return the serial number of the device."""
        return self._device["serial_id"]

    @property
    def away(self) -> bool:
        """Is the device in away mode."""
        return self._away

    async def set_away_status(self, away: bool) -> None:
        """Set the away status."""
        await self._session.set_device_away_status(self.dev_id, {"away": away})
        self._away_status_update(away_status={"away": away})

    @property
    def power_limit(self) -> int:
        """Get the power limit of the device."""
        return self._power_limit

    async def set_power_limit(self, power_limit: int) -> None:
        """Set the power limit of the device."""
        await self._session.set_device_power_limit(self.dev_id, power_limit)
        self._power_limit = power_limit


class SmartboxNode:
    """Smartbox Node."""

    def __init__(
        self,
        device: SmartboxDevice,
        node_info: Node,
        session: AsyncSmartboxSession,
        status: StatusDict,
        setup: SetupDict,
        samples: SamplesDict,
        version: dict[str, str],
        prog: ProgDict | None = None,
    ) -> None:
        """Initialise a smartbox node."""
        self._device = device
        self._node_info = node_info
        self._session = session
        self._status = status
        self._setup = setup
        self._samples = samples
        self._version = version
        self._prog = prog

    @classmethod
    async def create(
        cls,
        device: SmartboxDevice,
        session: AsyncSmartboxSession,
        node_info: Node,
    ) -> SmartboxNode:
        """Create a smartbox node."""
        status: StatusDict
        if node_info["type"] != SmartboxNodeType.PMO:
            status = cast(
                "StatusDict", await session.get_node_status(device.dev_id, node_info)
            )
        else:
            status = {
                "sync_status": "ok",
                "locked": False,
                "power": await session.get_device_power_limit(device.dev_id, node_info),
            }
        setup: SetupDict = cast(
            "SetupDict", await session.get_node_setup(device.dev_id, node_info)
        )
        samples: SamplesDict = cast(
            "dict[str, Any]",
            (
                await session.get_node_samples(
                    device.dev_id,
                    node_info,
                    int(time.time() - (3600 * 3)),
                    int(time.time()),
                )
            ),
        )["samples"]
        version: dict[str, str] = cast("dict[str, str]", await session.get_node_version(device.dev_id, node_info))
        prog: ProgDict | None = None
        if node_info["type"] in HEATER_NODE_TYPES:
            try:
                prog = _normalize_prog(
                    await session.get_node_prog(device.dev_id, node_info)
                )
            except APIUnavailableError:
                # Transient: setup retries via ConfigEntryNotReady.
                raise
            except SmartboxError:
                # No schedule support (unverified on non-htr families).
                _LOGGER.debug("Node %s does not support a schedule", node_info.get("name"))
        return cls(device, node_info, session, status, setup, samples, version, prog)

    @property
    def node_info(self) -> Node:
        """Return the node info."""
        return self._node_info

    @property
    def node_id(self) -> str:
        """Return the id of the node."""
        return f"{self._device.dev_id}_{self._node_info['addr']}"

    @property
    def name(self) -> str:
        """Return the name of the node."""
        return self._node_info["name"] or self.device.name

    @property
    def node_type(self) -> str:
        """Return node type, e.g. 'htr' for heaters."""
        return self._node_info["type"]

    @property
    def addr(self) -> int:
        """Return the addr of node."""
        return self._node_info["addr"]

    @property
    def status(self) -> StatusDict:
        """Return the status of node."""
        return self._status
    @property
    def version(self) -> dict[str, str]:
        """Version info of node (includes PID)."""
        return self._version

    @property
    def hw_version(self) -> str | None:
        """Product ID of the node."""
        return self._version.get("hw_version")

    @property
    def pid(self) -> str | None:
        """Product ID of the node."""
        return self._version.get("pid")

    def get_model_code(self) -> str | None:
        """Get the model code from the PID.

        Uses the last two characters of the PID, uppercased, matching the
        official app behaviour for 4-character PIDs
        (JS: pid.toUpperCase().slice(2, 4)).
        For PID '081c', this returns '1C'.
        """
        pid_min_length = 2
        pid = self.pid
        if pid and len(pid) >= pid_min_length:
            return pid[-pid_min_length:].upper()
        return None

    def update_status(self, status: StatusDict) -> None:
        """Update status."""
        _LOGGER.debug("Updating node %s status: %s", self.name, status)
        self._status |= {**status}

    @property
    def prog(self) -> ProgDict | None:
        """The node's weekly schedule, or None when it has none (yet)."""
        return self._prog

    def update_prog(self, prog: ProgDict) -> None:
        """Update the local schedule cache."""
        _LOGGER.debug("Updating node %s prog: %s", self.name, prog)
        self._prog = prog

    async def async_refresh_prog(self) -> ProgDict | None:
        """Fetch the schedule from the API.

        Raw mode returns the wire payload ``{"prog": {...}, "sync_status":
        ...}``; returns None (keeping any cached schedule) when the payload is
        unexpected or out of sync. Transient API failures propagate so entity
        polls can mark themselves unavailable.
        """
        response = await self._session.get_node_prog(
            self._device.dev_id, self._node_info
        )
        if not isinstance(response, dict) or response.get("sync_status", "ok") != "ok":
            return None
        return _normalize_prog(response)

    async def set_prog(self, prog: ProgDict) -> None:
        """Set (part of) the weekly schedule.

        The library GETs the current schedule, merges the given days over it
        and POSTs the complete object, so partial day updates are safe. The
        local cache is merged optimistically: writes are not immediately
        visible to GET (several seconds of device read-through lag).
        """
        await self._session.set_node_prog(
            self._device.dev_id, self._node_info, {"prog": prog}
        )
        if self._prog is not None:
            self._prog = {**self._prog, **prog}

    @property
    def setup(self) -> SetupDict:
        """Setup of node."""
        return self._setup

    def update_setup(self, setup: SetupDict) -> None:
        """Update setup."""
        _LOGGER.debug("Updating node %s setup: %s", self.name, setup)
        self._setup = setup

    async def set_status(self, **status_args: Any) -> StatusDict:  # noqa: ANN401
        """Set status."""
        await self._session.set_node_status(
            self._device.dev_id, self._node_info, status_args
        )
        # update our status locally until we get an update
        self._status |= {**status_args}
        return self._status

    @property
    def away(self) -> bool:
        """Is away mode."""
        return self._device.away

    @property
    def device(self) -> SmartboxDevice:
        """Return the device of the node."""
        return self._device

    @property
    def session(self) -> AsyncSmartboxSession:
        """Return the smartbox session."""
        return self._session

    async def update_device_away_status(self, away: bool) -> None:
        """Update device away status."""
        await self._device.set_away_status(away)

    async def async_update(self, _: Any) -> StatusDict:  # noqa: ANN401
        """Update status."""
        return self.status

    @property
    def window_mode(self) -> bool:
        """Is windows mode enable."""
        if "window_mode_enabled" not in self._setup:
            msg = f"window_mode_enabled not present in setup for node {self.name}"
            raise KeyError(msg)
        return self._setup["window_mode_enabled"]

    async def set_window_mode(self, window_mode: bool) -> bool:
        """Set window mode."""
        await self._session.set_node_setup(
            self._device.dev_id,
            self._node_info,
            {"window_mode_enabled": window_mode},
        )
        self._setup["window_mode_enabled"] = window_mode
        return window_mode

    @property
    def true_radiant(self) -> bool:
        """Is a true radiant."""
        if "true_radiant_enabled" not in self._setup:
            msg = f"true_radiant_enabled not present in setup for node {self.name}"
            raise KeyError(msg)
        return self._setup["true_radiant_enabled"]

    async def set_true_radiant(self, true_radiant: bool) -> None:
        """Set true radiant."""
        await self._session.set_node_setup(
            self._device.dev_id,
            self._node_info,
            {"true_radiant_enabled": true_radiant},
        )
        self._setup["true_radiant_enabled"] = true_radiant

    async def set_extra_options(self, options: dict[str, Any]) -> None:
        """Set extra options, preserving the other existing options.

        The node setup holds a single extra_options object; always send the
        merged object so unrelated keys are not wiped by the API.
        """
        extra_options: dict[str, Any] = {
            **self._setup.get("extra_options", {}),
            **options,
        }
        await self._session.set_node_setup(
            self._device.dev_id,
            self._node_info,
            {"extra_options": extra_options},
        )
        self._setup["extra_options"] = extra_options

    def is_heating(self, status: dict[str, Any]) -> bool:
        """Is heating."""
        return (
            status["charging"]
            if self.node_type == SmartboxNodeType.ACM
            else status["active"]
        )

    async def update_power(self) -> None:
        """Update power."""
        self._status["power"] = await self._session.get_device_power_limit(
            self.device.dev_id,
            self._node_info,
        )

    async def update_samples(self) -> None:
        """Update the samples."""
        max_sample = 2
        sample = await self.get_samples(
            int(time.time() - (3600 * 3)),
            int(time.time()),
        )
        if len(sample) >= max_sample:
            self._samples = sample[-2:]
            _LOGGER.debug("Updating node %s samples: %s", self.name, self._samples)

    async def get_samples(self, start_time: int, end_time: int) -> SamplesDict:
        """Update the samples."""
        return (
            cast(
                "dict[str,Any]",
                await self._session.get_node_samples(
                    self.device.dev_id,
                    self._node_info,
                    start_time,
                    end_time,
                ),
            )
        )["samples"]

    @property
    def total_energy(self) -> float | None:
        """Get the energy used."""
        if not self._samples:
            return None
        return self._samples[-1]["counter"]

    @property
    def boost_config(self) -> BoostConfig:
        """Get the boost config."""
        _boost_config = self._setup.get("factory_options", {}).get("boost_config", 0)
        return BoostConfig(_boost_config)

    @property
    def boost(self) -> bool:
        """Boost status."""
        return self.status.get("boost", False)

    @property
    def boost_available(self) -> bool:
        """Is boost available."""
        return bool(self.boost_config.value)

    @property
    def heater_node(self) -> bool:
        """Is this node a heater."""
        return self.node_type in HEATER_NODE_TYPES

    @property
    def boost_time(self) -> float:
        """Get the boost time."""
        return float(
            self.setup.get("extra_options", {}).get("boost_time", DEFAULT_BOOST_TIME)
        )

    @property
    def boost_temp(self) -> float:
        """Get the boost time."""
        return float(
            self.setup.get("extra_options", {}).get("boost_temp", DEFAULT_BOOST_TEMP)
        )

    @property
    def boost_end_min(self) -> int:
        """Get the boost end time."""
        return self.status.get("boost_end_min", 0)

    @property
    def remaining_boost_time(self) -> int:
        """Return the remaining boost time."""
        if not self.boost:
            return 0
        boost_end = get_boost_end_datetime(self.boost_end_min)
        return int((boost_end - dt_util.now()).total_seconds())


def get_temperature_unit(status: StatusDict) -> None | UnitOfTemperature:
    """Get the unit of temperature."""
    if "units" not in status:
        return None
    unit = status["units"]
    if unit == "C":
        return UnitOfTemperature.CELSIUS
    if unit == "F":
        return UnitOfTemperature.FAHRENHEIT
    msg = f"Unknown temp unit {unit}"
    raise ValueError(msg)


def get_boost_end_datetime(boost_end_min: int) -> datetime:
    """Return the local datetime at which the node boost ends.

    boost_end_min is the minute of the day (UTC) at which the boost ends, as
    reported by the API status. Returns a future datetime (the next day if the
    boost already ended earlier in the current UTC day).
    """
    now = dt_util.utcnow()
    hour, minute = divmod(int(boost_end_min), 60)
    boost_end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if boost_end <= now:
        # Boost already ended for the current UTC day: it ends tomorrow.
        boost_end += timedelta(days=1)
    return dt_util.as_local(boost_end)


async def get_devices(
    session: AsyncSmartboxSession, hass: HomeAssistant
) -> list[SmartboxDevice]:
    """Get the devices."""
    homes: list[dict[str, Any]] = cast(
        "list[dict[str, Any]]", await session.get_homes()
    )
    devices: list[SmartboxDevice] = []
    try:
        for home in homes:
            _home = home.copy()
            del _home["devs"]
            for session_device in home["devs"]:
                session_device["home"] = _home
                devices.append(
                    await SmartboxDevice.initialise_nodes(session_device, session, hass)
                )
    except Exception:
        # Every device created so far already runs websocket/update tasks;
        # cancel them so a failed or retried setup does not leak sessions.
        for device in devices:
            with suppress(Exception):
                await device.cancel()
        raise
    return devices


def _check_status_key(key: str, node_type: str, status: dict[str, Any]) -> None:
    if key not in status:
        msg = (
            f"'{key}' not found in {node_type} - please report to {GITHUB_ISSUES_URL}. "
            f"status: {status}"
        )
        raise KeyError(msg)


def get_target_temperature(node_type: str, status: dict[str, Any]) -> float:
    """Get the target temperature."""
    if node_type == SmartboxNodeType.HTR_MOD:
        _check_status_key("selected_temp", node_type, status)
        if status["selected_temp"] == "comfort":
            _check_status_key("comfort_temp", node_type, status)
            return float(status["comfort_temp"])
        if status["selected_temp"] == "eco":
            _check_status_key("comfort_temp", node_type, status)
            _check_status_key("eco_offset", node_type, status)
            return float(status["comfort_temp"]) - float(status["eco_offset"])
        if status["selected_temp"] == "ice":
            _check_status_key("ice_temp", node_type, status)
            return float(status["ice_temp"])
        if status["selected_temp"] == "off":
            return float(0)
        msg = (
            f"Unexpected 'selected_temp' value {status['selected_temp']}"
            f" found for {node_type} - please report to"
            f" {GITHUB_ISSUES_URL}. status: {status}"
        )
        raise KeyError(msg)
    _check_status_key("stemp", node_type, status)
    return float(status["stemp"])


def set_temperature_args(
    node_type: str, status: dict[str, Any], temp: float
) -> dict[str, Any]:
    """Set targeted temperature."""
    _check_status_key("units", node_type, status)
    if node_type == SmartboxNodeType.HTR_MOD:
        if status["selected_temp"] == "comfort":
            target_temp = temp
        elif status["selected_temp"] == "eco":
            _check_status_key("eco_offset", node_type, status)
            target_temp = temp + float(status["eco_offset"])
        elif status["selected_temp"] == "ice":
            msg = "Can't set temperature for htr_mod devices when ice mode is selected"
            raise ValueError(msg)
        else:
            msg = (
                f"Unexpected 'selected_temp' value {status['selected_temp']}"
                f" found for {node_type} - please report to "
                f"{GITHUB_ISSUES_URL}. status: {status}"
            )
            raise KeyError(msg)
        return {
            "on": True,
            "mode": status["mode"],
            "selected_temp": status["selected_temp"],
            "comfort_temp": str(target_temp),
            "eco_offset": status["eco_offset"],
            "units": status["units"],
        }
    return {
        "stemp": str(temp),
        "units": status["units"],
    }


def get_hvac_mode(node_type: str, status: dict[str, Any]) -> HVACMode | None:
    """Get the mode of HVAC."""
    if status.get("boost", False):
        return HVACMode.HEAT
    _check_status_key("mode", node_type, status)
    if status["mode"] == "off" or (
        node_type == SmartboxNodeType.HTR_MOD and not status["on"]
    ):
        return HVACMode.OFF
    if status["mode"] == "manual":
        return HVACMode.HEAT
    if status["mode"] == "auto":
        return HVACMode.AUTO
    if status["mode"] == "modified_auto":
        # This occurs when the temperature is modified while in auto mode.
        # Mapping it to auto seems to make this most sense
        return HVACMode.AUTO
    if status["mode"] == "self_learn" or status["mode"] == "presence":
        return HVACMode.AUTO
    msg = f"Unknown smartbox node mode {status['mode']}"
    _LOGGER.error(msg)
    raise ValueError(msg)


def set_hvac_mode_args(
    node_type: str, status: dict[str, Any], hvac_mode: str
) -> dict[str, Any]:
    """Set the mode of HVAC."""
    error_msg = f"Unsupported hvac mode {hvac_mode}"
    if node_type == SmartboxNodeType.HTR_MOD:
        if hvac_mode == HVACMode.OFF:
            return {"on": False}
        if hvac_mode == HVACMode.HEAT:
            # We need to pass these status keys on when setting the mode
            required_status_keys = ["selected_temp"]
            for key in required_status_keys:
                _check_status_key(key, node_type, status)
            hvac_mode_args = {k: status[k] for k in required_status_keys}
            hvac_mode_args["on"] = True
            hvac_mode_args["mode"] = "manual"
            return hvac_mode_args
        if hvac_mode == HVACMode.AUTO:
            return {"on": True, "mode": "auto"}
        raise ValueError(error_msg)
    if hvac_mode == HVACMode.OFF:
        return {"mode": "off"}
    if hvac_mode == HVACMode.HEAT:
        return {"mode": "manual"}
    if hvac_mode == HVACMode.AUTO:
        return {"mode": "auto"}
    raise ValueError(error_msg)


def set_preset_mode_status_update(
    node_type: str, status: dict[str, Any], preset_mode: str
) -> dict[str, Any]:
    """Set preset mode status update."""
    if node_type != SmartboxNodeType.HTR_MOD:
        msg = f"{node_type} nodes do not support preset {preset_mode}"
        raise ValueError(msg)
    # PRESET_HOME and PRESET_AWAY are not handled via status updates
    assert preset_mode not in (PRESET_HOME, PRESET_AWAY, PRESET_NONE)  # noqa: S101

    if preset_mode == PRESET_SCHEDULE:
        return set_hvac_mode_args(node_type, status, HVACMode.AUTO)
    if preset_mode == PRESET_SELF_LEARN:
        return {"on": True, "mode": "self_learn"}
    if preset_mode == PRESET_ACTIVITY:
        return {"on": True, "mode": "presence"}
    if preset_mode == PRESET_COMFORT:
        return {"on": True, "mode": "manual", "selected_temp": "comfort"}
    if preset_mode == PRESET_ECO:
        return {"on": True, "mode": "manual", "selected_temp": "eco"}
    if preset_mode == PRESET_FROST:
        return {"on": True, "mode": "manual", "selected_temp": "ice"}
    msg = f"Unsupported preset {preset_mode} for node type {node_type}"
    raise ValueError(msg)


def get_factory_options(node: SmartboxNode) -> FactoryOptionsDict:
    """Get the factory options."""
    return cast("FactoryOptionsDict", node.setup.get("factory_options", {}))


def window_mode_available(node: SmartboxNode) -> bool:
    """Is window mode available."""
    return get_factory_options(node).get("window_mode_available", False)


def true_radiant_available(node: SmartboxNode) -> bool:
    """Is true radiant available."""
    return get_factory_options(node).get("true_radiant_available", False)


def _normalize_prog(payload: Any) -> ProgDict | None:  # noqa: ANN401
    """Validate and normalize a schedule payload into day-keyed slot lists.

    Accepts both the REST shape ``{"prog": {...}, "sync_status": ...}`` and
    a bare day-keyed object; returns None when the shape is unexpected. Day
    keys must be strings and each value a list of ints.
    """
    if isinstance(payload, dict) and isinstance(payload.get("prog"), dict):
        payload = payload["prog"]
    if not isinstance(payload, dict):
        return None
    prog: ProgDict = {}
    for day, slots in payload.items():
        if not (
            isinstance(day, str)
            and isinstance(slots, list)
            and all(isinstance(slot, int) for slot in slots)
        ):
            return None
        prog[day] = list(slots)
    return prog


def get_current_prog_profile(prog: ProgDict, now: datetime) -> int | None:
    """Return the profile index the schedule asks for at local time ``now``.

    Day keys "0".."6" run Monday..Sunday. Slot length is 24 h divided by the
    day-array length (24 hourly slots, or 48 half-hourly where supported).
    """
    day = prog.get(str(now.weekday()))
    if not day:
        return None
    slot_minutes = _PROG_DAY_MINUTES // len(day)
    if slot_minutes < 1:
        return None
    slot = (now.hour * 60 + now.minute) // slot_minutes
    return day[min(slot, len(day) - 1)]


def get_next_prog_change(prog: ProgDict, now: datetime) -> datetime | None:
    """Return the local datetime at which the schedule next changes profile.

    Walks slot boundaries forward from ``now``, wrapping after Sunday; None
    when the programme never changes. ``now`` must be an aware datetime (its
    timezone defines "local"); DST shifts are handled best-effort.
    """
    current = get_current_prog_profile(prog, now)
    local_now = dt_util.as_local(now)
    for day_offset in range(8):
        day = prog.get(str((local_now.weekday() + day_offset) % 7))
        if not day:
            continue
        slot_minutes = _PROG_DAY_MINUTES // len(day)
        if slot_minutes < 1:
            continue
        day_start = (local_now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for slot, profile in enumerate(day):
            start = dt_util.as_local(
                day_start + timedelta(minutes=slot * slot_minutes)
            )
            if start <= local_now:
                continue
            if profile != current:
                return start
    return None


def resolve_target_entity_ids(hass: HomeAssistant, data: dict[str, Any]) -> set[str]:
    """Resolve a service call's targets (entity/device/area) to entity ids."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    target_entity_ids: set[str] = set(data.get(ATTR_ENTITY_ID, []))
    for device_id in data.get(ATTR_DEVICE_ID, []):
        target_entity_ids.update(
            entity.entity_id
            for entity in er.async_entries_for_device(entity_registry, device_id)
        )
    for area_id in data.get(ATTR_AREA_ID, []):
        for device in dr.async_entries_for_area(device_registry, area_id):
            target_entity_ids.update(
                entity.entity_id
                for entity in er.async_entries_for_device(
                    entity_registry, device.id
                )
            )
    return target_entity_ids

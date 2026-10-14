"""Support for Smartbox climate entities.

Entities read state through the adapter's accessors over ``runtime.engine`` and
write through its named ops; updates arrive via the shared
``EngineChangeFanOut``, one registration per node. Setup frames refresh the
entity too, so the max_temp clamp tracks ``max_stemp_limit`` without a restart.
The mapping helpers live in ``mapping.py``; the REST discovery identity is kept
only for registry identity and device-info fields.
"""

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import DEFAULT_MAX_TEMP, DEFAULT_MIN_TEMP
from homeassistant.const import ATTR_LOCKED, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter

from .adapter import EngineChangeFanOut, SmartboxNodeAdapterEntity, node_max_stemp_limit
from .const import (
    GITHUB_ISSUES_URL,
    PRESET_FROST,
    PRESET_SCHEDULE,
    PRESET_SELF_LEARN,
    SmartboxNodeType,
)
from .identity import boost_available
from .mapping import (
    AutoOverrideWrite,
    HtrModWrite,
    check_status_key,
    get_hvac_mode,
    get_target_temperature,
    get_temperature_unit,
    hvac_mode_write,
    preset_mode_write,
    temperature_write,
)
from .smartbox_protocol.values import bounded_repr, is_lost

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import SmartboxConfigEntry, SmartboxRuntime
    from .identity import SmartboxNodeInfo

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - HA platform signature
    entry: SmartboxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up platform."""
    _LOGGER.debug("Setting up Smartbox climate platform")

    runtime = entry.runtime_data
    fanout = runtime.fanout
    async_add_entities(
        [
            SmartboxHeater(node, entry, runtime, fanout)
            for node in runtime.identity.nodes
            if node.heater_node
        ]
    )
    _LOGGER.debug("Finished setting up Smartbox climate platform")


class SmartboxHeater(SmartboxNodeAdapterEntity, ClimateEntity):
    """Smartbox heater climate control."""

    _attr_name = None
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(
        self,
        node: SmartboxNodeInfo,
        entry: SmartboxConfigEntry,
        runtime: SmartboxRuntime,
        fanout: EngineChangeFanOut,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(node, entry, runtime, fanout, "thermostat")

    @property
    def _lost(self) -> bool:
        """The status slot is a bare lost marker.

        The engine never stores one (a lost-marked payload is availability
        evidence only), so this is the entity's own protection: a marker in the
        status carries none of the required keys, and property evaluation must
        render None rather than raise.
        """
        return is_lost(self._status)

    @property
    def _status_blank(self) -> bool:
        """True while no status body has arrived yet.

        Availability does not imply a body: a setup or prog write can confirm
        its own write, marking the node available, before any status frame is
        seen. Reads needing a status key must then render None.
        """
        return not self._status

    @property
    def _away(self) -> bool:
        """Device away flag; PRESET_HOME until the wire delivered away."""
        device = self._device_state()
        return bool((device.away if device is not None else {}).get("away", False))

    @property
    def _boost(self) -> bool:
        """Boost flag from the node status (True while boost is running)."""
        return bool(self._status.get("boost", False))

    @property
    def _boost_available(self) -> bool:
        """Is boost available on this node (see ``identity.boost_available``)."""
        state = self._node_state()
        if state is None:
            return False
        return boost_available(state.setup)

    async def async_turn_off(self) -> None:
        """Turn off hvac."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        """Turn on hvac."""
        await self.async_set_hvac_mode(HVACMode.AUTO)

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        unit = get_temperature_unit(self._status)
        if unit is not None:
            return unit
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        if self._lost or self._status_blank:
            return None
        return float(self._status["mtemp"])

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        if self._lost or self._status_blank:
            return None
        return get_target_temperature(self._node.node_type, self._status)

    @property
    def target_temperature_step(self) -> float:
        """Setpoint grid step: 0.5 for Celsius, 1.0 for Fahrenheit.

        Matches the official app. Without it HA's UI emits off-grid setpoints
        that the device silently quantizes, so the optimistic UI value differs
        from what the device stored.
        """
        return 0.5 if self.temperature_unit == UnitOfTemperature.CELSIUS else 1.0

    @property
    def min_temp(self) -> float:
        """HA's default minimum in the device's scale.

        A plain property on purpose: ``ClimateEntity.min_temp`` is a
        cached_property, and this entity is added before the store has any
        state, so the inherited cache would freeze the Celsius default for
        Fahrenheit devices.
        """
        return TemperatureConverter.convert(
            DEFAULT_MIN_TEMP, UnitOfTemperature.CELSIUS, self.temperature_unit
        )

    @property
    def max_temp(self) -> float:
        """Maximum settable temperature, clamped by the device's limit.

        The fw-1.9 family reports ``max_stemp_limit`` in setup, in the device's
        scale; "0.0" means no user limit, so HA's default maximum applies.
        Refreshed on setup frames via the fan-out. Plain property: see min_temp.
        """
        if (limit := node_max_stemp_limit(self._node_state())) is not None:
            return limit
        return TemperatureConverter.convert(
            DEFAULT_MAX_TEMP, UnitOfTemperature.CELSIUS, self.temperature_unit
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Set new target temperature."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        state = self._node_state()
        write = temperature_write(
            self._node.node_type,
            self._status,
            temp,
            state.setup if state is not None else {},
        )
        engine = self._runtime.engine
        if isinstance(write, HtrModWrite):
            await engine.set_htr_mod_status(
                self._dev_id,
                self._ref,
                on=write.on,
                mode=write.mode,
                selected_temp=write.selected_temp,
                comfort_temp=write.comfort_temp,
                eco_offset=write.eco_offset,
                units=write.units,
            )
        elif isinstance(write, AutoOverrideWrite):
            # App-shaped override for setpoints inside the running programme
            # (temperature_write's gated condition).
            await engine.engage_modified_auto(
                self._dev_id, self._ref, temp, write.units
            )
        else:
            await engine.set_setpoint(self._dev_id, self._ref, temp, write.units)

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current operation ie. heat or idle."""
        node_type = self._node.node_type
        status = self._status
        if self._lost or self._status_blank:
            return None
        if status["charging" if node_type == SmartboxNodeType.ACM else "active"]:
            return HVACAction.HEATING
        if (
            status["mode"] == "off"
            or (node_type == SmartboxNodeType.HTR_MOD and not status["on"])
        ) and not self._boost:
            return HVACAction.OFF
        return HVACAction.IDLE

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac target hvac state."""
        if self._lost or self._status_blank:
            return None
        return get_hvac_mode(self._node.node_type, self._status)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available operation modes."""
        return [HVACMode.HEAT, HVACMode.AUTO, HVACMode.OFF]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set operation mode."""
        _LOGGER.debug("Setting HVAC mode to %s", hvac_mode)
        write = hvac_mode_write(self._node.node_type, self._status, hvac_mode)
        engine = self._runtime.engine
        if isinstance(write, HtrModWrite):
            await engine.set_htr_mod_status(
                self._dev_id,
                self._ref,
                on=write.on,
                mode=write.mode,
                selected_temp=write.selected_temp,
            )
        else:
            await engine.set_mode(self._dev_id, self._ref, write.mode)
        # Leaving boost is its own accepted write: the engine's named ops are
        # single-key.
        if self._boost:
            await engine.set_boost(self._dev_id, self._ref, on=False)

    def _preset_mode_for_manual(self) -> str:
        check_status_key("selected_temp", self._node.node_type, self._status)
        selected_temp = self._status["selected_temp"]
        if selected_temp == "comfort":
            return PRESET_COMFORT
        if selected_temp == "eco":
            return PRESET_ECO
        if selected_temp == "ice":
            return PRESET_FROST
        msg = (
            f"Unexpected 'selected_temp' value {bounded_repr(selected_temp)} found for "
            f"{self._node.node_type} and manual - please report to {GITHUB_ISSUES_URL}."
        )
        raise ValueError(msg)

    @property
    def preset_mode(self) -> str | None:  # noqa: PLR0911
        """Get preset mode."""
        if self._lost:
            return None
        if self._away:
            return PRESET_AWAY
        if self._boost:
            return PRESET_BOOST
        if self._node.node_type == SmartboxNodeType.HTR_MOD:
            # This node type's preset IS the status mode, so a node whose
            # status has not arrived yet has no preset to report.
            if self._status_blank:
                return None
            check_status_key("mode", self._node.node_type, self._status)
            mode = self._status["mode"]
            if mode == "auto":
                return PRESET_SCHEDULE
            if mode == "presence":
                return PRESET_ACTIVITY
            if mode == "self_learn":
                return PRESET_SELF_LEARN
            if mode == "manual":
                return self._preset_mode_for_manual()
            msg = f"Unknown smartbox node mode {bounded_repr(mode)}"
            raise ValueError(msg)
        return PRESET_HOME

    @property
    def preset_modes(self) -> list[str]:
        """Get the preset_modes."""
        default_preset_modes = [PRESET_AWAY]
        if self._boost_available:
            default_preset_modes.append(PRESET_BOOST)
        if self._node.node_type == SmartboxNodeType.HTR_MOD:
            default_preset_modes.extend(
                [
                    PRESET_ACTIVITY,
                    PRESET_COMFORT,
                    PRESET_ECO,
                    PRESET_FROST,
                    PRESET_SELF_LEARN,
                    PRESET_SCHEDULE,
                ]
            )
        else:
            default_preset_modes.append(PRESET_HOME)
        return default_preset_modes

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Return the state attributes of the device.

        A lost body carries no locked key: it is OMITTED (no stale last-
        known value is kept - the entity is unavailable while lost, and
        the next ok frame restores the attribute).
        """
        if self._lost or self._status_blank:
            return {}
        return {
            ATTR_LOCKED: self._status["locked"],
        }

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the mode."""
        engine = self._runtime.engine
        if preset_mode == PRESET_AWAY:
            await engine.set_away(self._dev_id, away=True)
            return
        if self._away:
            await engine.set_away(self._dev_id, away=False)
        if preset_mode == PRESET_BOOST:
            await engine.set_boost(self._dev_id, self._ref, on=True)
            return
        if self._node.node_type == SmartboxNodeType.HTR_MOD:
            write = preset_mode_write(self._node.node_type, self._status, preset_mode)
            if isinstance(write, HtrModWrite):
                await engine.set_htr_mod_status(
                    self._dev_id,
                    self._ref,
                    on=write.on,
                    mode=write.mode,
                    selected_temp=write.selected_temp,
                )
            else:
                await engine.set_mode(self._dev_id, self._ref, write.mode)

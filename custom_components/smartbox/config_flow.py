"""Config flow for Smartbox.

Validation runs on the protocol layer: build the AuthConfig from the entry
data, run one password grant, then the one-shot ``ws_user`` capability probe.
The UI error mapping is cannot_connect / invalid_auth / unknown, plus
``unsupported`` for a deterministic probe rejection - there is no socket_io
fallback, so such a host cannot work.
"""

import logging
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowContext,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import SmartboxConfigEntry, auth_config_from_entry_data
from .compat import vol
from .const import (
    CONF_API_NAME,
    CONF_DISPLAY_ENTITY_PICTURES,
    CONF_HISTORY_CONSUMPTION,
    CONF_TIMEDELTA_POWER,
    DEFAULT_TIMEDELTA_POWER,
    DOMAIN,
    HistoryConsumptionStatus,
)
from .smartbox_protocol import (
    AuthConfig,
    AuthError,
    SmartboxError,
    TokenManager,
    TransportUnavailableError,
    TransportUnsupportedError,
    check_ws_user_support,
)
from .smartbox_protocol.reseller import AvailableResellers

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

LOGIN_DATA_SCHEMA = {
    vol.Required(CONF_USERNAME): TextSelector(
        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
    ),
    vol.Required(CONF_PASSWORD): TextSelector(
        TextSelectorConfig(
            type=TextSelectorType.PASSWORD,
            autocomplete="current-password",
        )
    ),
}

OPTIONS_DATA_SCHEMA = {
    vol.Required(CONF_HISTORY_CONSUMPTION): SelectSelector(
        SelectSelectorConfig(
            options=[e.value for e in HistoryConsumptionStatus],
            mode=SelectSelectorMode.DROPDOWN,
        )
    ),
    vol.Required(CONF_DISPLAY_ENTITY_PICTURES, default=False): BooleanSelector(),
    vol.Required(
        CONF_TIMEDELTA_POWER, default=DEFAULT_TIMEDELTA_POWER
    ): vol.All(cv.positive_int, vol.Range(min=1)),
}


def _step_user_data_schema() -> vol.Schema:
    """Build the account form from the current reseller table.

    Built per render so a reseller-table change (and a test that patches it)
    is reflected in the select options and their validation.
    """
    return vol.Schema(
        {
            vol.Required(CONF_API_NAME): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=key, label=reseller.name)
                        for key, reseller in AvailableResellers.resellers.items()
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            **LOGIN_DATA_SCHEMA,
        },
    )


async def async_validate_credentials(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> None:
    """Validate credentials on the protocol layer.

    One fresh password grant plus the ``ws_user`` capability probe, on a
    throwaway TokenManager that shares HA's clientsession. Protocol-layer
    errors propagate RAW for the flow steps to map to their UI error strings.
    """
    config: AuthConfig = auth_config_from_entry_data(user_input)
    manager = TokenManager(config, session=async_get_clientsession(hass))
    token_bundle = await manager.get_token()
    await check_ws_user_support(
        token_bundle.access_token, config.api_host, manager.session
    )


def _entry_unique_id(api_url: str, username: str) -> str:
    """Build the config entry unique id (shared by the user + reauth steps).

    Resolves through the reseller table, so an aliased reseller
    (api-technoterm -> api-lhz) yields the resolved api_url, matching the
    unique ids the released integration created.
    """
    return f"{AvailableResellers(api_url=api_url).api_url}_{username}"


async def _handle_credentials(
    hass: HomeAssistant,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, str],
) -> bool:
    """Run validation; fill errors/placeholders. True when it succeeded.

    The UI error mapping follows the protocol-layer exception types.
    """
    try:
        await async_validate_credentials(hass, user_input)
    except TransportUnavailableError as ex:
        errors["base"] = "cannot_connect"
        placeholders["error"] = str(ex)
    except AuthError as ex:
        errors["base"] = "invalid_auth"
        placeholders["error"] = str(ex)
    except TransportUnsupportedError as ex:
        errors["base"] = "unsupported"
        placeholders["error"] = str(ex)
    except SmartboxError as ex:
        errors["base"] = "unknown"
        placeholders["error"] = str(ex)
    else:
        return True
    return False


class SmartboxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Smartbox."""

    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None and await _handle_credentials(
            self.hass, user_input, errors, placeholders
        ):
            api_url = user_input[CONF_API_NAME]
            await self.async_set_unique_id(
                _entry_unique_id(api_url, user_input[CONF_USERNAME])
            )
            self._abort_if_unique_id_configured()
            # AvailableResellers is used ONLY for the display name here.
            return self.async_create_entry(
                title=f"{AvailableResellers(api_url=api_url).name} "
                f"{user_input[CONF_USERNAME]}",
                data=user_input,
            )
        context = dict(self.context)
        context["title_placeholders"] = placeholders
        self.context = cast("ConfigFlowContext", context)
        return self.async_show_form(
            step_id="user",
            data_schema=_step_user_data_schema(),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the account credentials without removing the entry.

        Entity unique ids are node-based, so changing the reseller or username
        keeps the registry entries and their history. The update listener
        reloads the entry when the data changes.
        """
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None and await _handle_credentials(
            self.hass, user_input, errors, placeholders
        ):
            api_url = user_input[CONF_API_NAME]
            username = user_input[CONF_USERNAME]
            unique_id = _entry_unique_id(api_url, username)
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, unique_id
            )
            if existing is not None and existing.entry_id != entry.entry_id:
                return self.async_abort(reason="already_configured")
            self.hass.config_entries.async_update_entry(
                entry,
                unique_id=unique_id,
                title=f"{AvailableResellers(api_url=api_url).name} {username}",
                data={**entry.data, **user_input},
            )
            return self.async_abort(reason="reconfigure_successful")

        context = dict(self.context)
        context["title_placeholders"] = placeholders
        self.context = cast("ConfigFlowContext", context)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _step_user_data_schema(),
                {
                    CONF_API_NAME: entry.data.get(CONF_API_NAME),
                    CONF_USERNAME: entry.data.get(CONF_USERNAME),
                },
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        self.current_user_inputs = entry_data.copy()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            user_input = {**self.current_user_inputs, **user_input}
            if await _handle_credentials(
                self.hass, user_input, errors, placeholders
            ):
                api_url = user_input[CONF_API_NAME]
                await self.async_set_unique_id(
                    _entry_unique_id(api_url, user_input[CONF_USERNAME])
                )
                self._abort_if_unique_id_mismatch(reason="invalid_auth")
                entry = self._get_reauth_entry()
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, **user_input}
                )
                # The entry's update listener owns the reload (HA flags the
                # update_reload_and_abort helper when a listener is present).
                return self.async_abort(reason="reauth_successful")
            # A failed attempt re-shows THIS step: the user step would create
            # an entry and abort on the entry's own unique id, stranding reauth
            # in already_configured. This form carries the username too, so a
            # changed account corrects through the mismatch abort above.
        context = dict(self.context)
        context["title_placeholders"] = placeholders
        self.context = cast("ConfigFlowContext", context)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(LOGIN_DATA_SCHEMA),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: SmartboxConfigEntry,
    ) -> OptionsFlow:
        """Create the options flow handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    """Options flow."""

    def __init__(self, config_entry: SmartboxConfigEntry) -> None:
        """Initialise class."""
        self.config_entry_options = config_entry.options

    async def async_step_init(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the Smartbox options."""
        return await self.async_step_options()

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title=None, data=user_input)

        return self.async_show_form(
            step_id="options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(OPTIONS_DATA_SCHEMA), self.config_entry_options
            ),
        )

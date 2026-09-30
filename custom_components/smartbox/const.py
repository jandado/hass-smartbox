"""Constants for the Smartbox integration."""

from enum import Enum, StrEnum

from smartbox import SmartboxNodeType

DOMAIN = "smartbox"

ATTR_DURATION = "duration"
SERVICE_SET_BOOST_PARAMS = "set_boost_params"
SERVICE_SET_SCHEDULE = "set_schedule"
FIELD_SCHEDULE = "schedule"
# Schedule profile slot values; day keys are "0".."6" (Monday..Sunday).
DAY_KEYS = ("0", "1", "2", "3", "4", "5", "6")
# Schedule profile indices for plain htr nodes, in the vendor app's
# sorted order (webapi-spec §5): 0=ICE, 1=ECO, 2=COMF. Unverified for
# acm/htr_mod families; unknown indices are reported as 'unknown'.
PROG_PROFILE_NAMES = ("frost", "eco", "comfort")
CONF_API_NAME = "api_name"
CONF_DISPLAY_ENTITY_PICTURES = "reseller_entity"
CONF_TIMEDELTA_POWER = "timedelta_update_power"

DEFAULT_TIMEDELTA_POWER = 60
DEFAULT_BOOST_TIME = 60
DEFAULT_BOOST_TEMP = 21.0
# Restore value when the maximum temperature limit is re-enabled with no
# remembered value (the wire erases max_stemp_limit on toggle-off); must
# match the number entity's Celsius slider cap in number.py.
MAX_TEMP_LIMIT_DEFAULT = 30.0
# Node-availability windows (seconds), live-probed 2026-09-30: the vendor
# app flags a node unreachable ~5-6 s after a command whose confirming
# frame never arrives; a bare lost frame is likewise given ~6 s to be
# followed by an ok frame. See ../smartbox api-notes.md "Node reachability".
SMARTBOX_UNAVAILABLE_DELAY = 6.0
SMARTBOX_WRITE_CONFIRM_TIMEOUT = 6.0
GITHUB_ISSUES_URL = "https://github.com/ajtudela/hass-smartbox/issues"

HEATER_NODE_TYPES = [
    SmartboxNodeType.ACM,
    SmartboxNodeType.HTR,
    SmartboxNodeType.HTR_MOD,
]


PRESET_FROST = "frost"
PRESET_SCHEDULE = "schedule"
PRESET_SELF_LEARN = "self_learn"

CONF_HISTORY_CONSUMPTION = "history_consumption"


class HistoryConsumptionStatus(StrEnum):
    """Config Consumption History Status."""

    START = "start"
    AUTO = "auto"
    OFF = "off"


class BoostConfig(Enum):
    """Boost configuration."""

    UNSUPPORTED = 0
    BASIC = 1
    FULL = 2

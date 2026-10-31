"""Validation-library compatibility for the supported HA range.

Home Assistant 2026.5, the integration's floor, uses voluptuous;
``probatio`` becomes the validation stack in the 2026.9/2026.10 cycle. The
selection follows the library HA's own ``config_validation`` is using, so
schemas and ``cv`` helpers cannot end up on different stacks.
"""

from __future__ import annotations

from homeassistant.helpers import config_validation as cv

if cv.positive_int.__module__.startswith("probatio"):
    import probatio as vol
else:  # HA before the probatio migration
    import voluptuous as vol  # type: ignore[no-redef]

__all__ = ["vol"]

"""Validation-library compatibility for the supported HA range.

Home Assistant 2026.5, the integration's floor, ships voluptuous. ``probatio``
becomes the validation stack in the 2026.9/2026.10 cycle, so both are
supported until the minimum Home Assistant is raised. This is the one module
that imports either; schemas use ``compat.vol``.
"""

from __future__ import annotations

try:
    import probatio as vol
except ImportError:  # HA before the probatio migration
    import voluptuous as vol  # type: ignore[no-redef]

__all__ = ["vol"]

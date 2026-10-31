"""Tests for validation-library selection."""

from homeassistant.helpers import config_validation as cv

from custom_components.smartbox.compat import vol


def test_validation_library_matches_home_assistant():
    """Schemas and HA's cv helpers must use the same validation stack."""
    assert vol.__name__ == cv.positive_int.__module__.split(".", 1)[0]

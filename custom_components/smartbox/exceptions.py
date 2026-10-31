"""Translated user-facing exceptions.

Every user-visible error carries a translation key from ``strings.json``'s
``exceptions`` section, so Home Assistant renders it in the caller's language.
``ServiceValidationError`` marks bad service input; ``HomeAssistantError``
marks a state restriction or a write that failed after validation.
"""

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN

__all__ = ["user_error", "validation_error"]


def user_error(key: str, **placeholders: str) -> HomeAssistantError:
    """Build a translated error for a state restriction or failed action."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def validation_error(key: str, **placeholders: str) -> ServiceValidationError:
    """Build a translated error for invalid service input."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )

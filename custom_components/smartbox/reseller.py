"""Reseller table access (config-entry data -> reseller identity)."""

from typing import TYPE_CHECKING

from smartbox.protocol.reseller import (
    API_HOST,
    AvailableResellers,
    SmartboxReseller,
    api_base_url,
)

from .const import CONF_API_NAME

__all__ = ["API_HOST", "api_base_url", "reseller_from_entry_data"]

if TYPE_CHECKING:
    from collections.abc import Mapping


def reseller_from_entry_data(data: Mapping[str, str]) -> SmartboxReseller:
    """Return the reseller table row for the entry's api_name."""
    return AvailableResellers(api_url=data[CONF_API_NAME]).reseller

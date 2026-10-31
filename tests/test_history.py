"""Tests for the first-import ledger."""

from custom_components.smartbox.const import (
    CONF_HISTORY_CONSUMPTION,
    HistoryConsumptionStatus,
)
from custom_components.smartbox.history import HistoryImport


def test_track_pends_only_when_imports_are_enabled():
    """An AUTO entry records a late node without a permanent pending import."""
    history = HistoryImport(
        [], {CONF_HISTORY_CONSUMPTION: HistoryConsumptionStatus.AUTO}
    )
    history.track("device_1_2")
    assert history.total_nodes == 1
    assert history.pending_nodes == ()
    assert history.completed_nodes == ("device_1_2",)


def test_track_pends_under_start():
    history = HistoryImport(
        [], {CONF_HISTORY_CONSUMPTION: HistoryConsumptionStatus.START}
    )
    history.track("device_1_2")
    assert history.pending_nodes == ("device_1_2",)
    assert history.completed_nodes == ()

"""Backoff helpers shared by the transport, REST client and token grants."""

from smartbox.protocol.retry import backoff_delay, jittered_backoff


def test_backoff_delay_saturates_instead_of_overflowing():
    """A very long outage sleeps the cap, not 2**attempt."""
    assert backoff_delay(1024, 1.0, 30.0) == 30.0


def test_jittered_backoff_stays_within_the_jitter_window():
    """Every delay is the bare base plus 0-20 %, never below it."""
    for attempt in range(6):
        base = backoff_delay(attempt, 1.0, 30.0)
        for _ in range(50):
            delay = jittered_backoff(attempt, 1.0, 30.0)
            # The epsilon absorbs float rounding at the 20 % upper bound.
            assert base <= delay <= min(base * 1.2, 30.0) + 1e-9


def test_jittered_backoff_respects_the_cap():
    """Jitter does not push a capped delay above the cap."""
    for _ in range(50):
        assert jittered_backoff(10, 1.0, 3.0) == 3.0

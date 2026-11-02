"""Shared retry/backoff primitives.

The websocket reconnect path and capability probe, the REST GET retries and
the token-grant ladder must not drift apart under the same failure, so the
capped exponential base and its one-sided jitter live here.
"""

from __future__ import annotations

import random

# Saturation bound for backoff_delay's exponent (see its docstring).
_MAX_BACKOFF_EXPONENT = 6

# One-sided jitter: a delay is never shorter than the bare exponential base.
_JITTER_FRACTION = 0.2
_JITTER_RNG = random.SystemRandom()


def backoff_delay(attempt: int, factor: float, cap: float) -> float:
    """Capped exponential backoff: ``factor * 2^attempt``, capped.

    The exponent saturates before the power: an unsaturated ``2**attempt``
    raises ``OverflowError`` once the caller's failure counter exceeds ~1024, so
    a multi-day outage sleeps the cap instead of crashing the retry loop.
    """
    return float(min(factor * (2 ** min(attempt, _MAX_BACKOFF_EXPONENT)), cap))


def jittered_backoff(attempt: int, factor: float, cap: float) -> float:
    """Capped exponential backoff plus one-sided +0-20 % jitter.

    The jitter desynchronises clients retrying after the same outage. It is
    added, never subtracted, so a delay is never below the bare base, and the
    final value stays within ``cap``.
    """
    base = backoff_delay(attempt, factor, cap)
    return min(base * _JITTER_RNG.uniform(1.0, 1.0 + _JITTER_FRACTION), cap)

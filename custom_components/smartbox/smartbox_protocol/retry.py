"""Shared retry/backoff primitive for the websocket transport.

The reconnect path and the capability probe must not drift apart under the same
failure, so the capped exponential backoff lives here.
"""

from __future__ import annotations

# Saturation bound for backoff_delay's exponent (see its docstring).
_MAX_BACKOFF_EXPONENT = 6


def backoff_delay(attempt: int, factor: float, cap: float) -> float:
    """Capped exponential backoff: ``factor * 2^attempt``, capped.

    The exponent saturates before the power: an unsaturated ``2**attempt``
    raises ``OverflowError`` once the caller's failure counter exceeds ~1024, so
    a multi-day outage sleeps the cap instead of crashing the retry loop.
    """
    return float(min(factor * (2 ** min(attempt, _MAX_BACKOFF_EXPONENT)), cap))

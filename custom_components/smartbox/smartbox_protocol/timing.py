"""Timing seams: the one place waits and clocks are substituted.

Every layer routes its waits through :data:`sleep` and its monotonic clock
reads through :data:`monotonic`, so a test that needs deterministic deadlines,
backoff or grace windows patches two attributes here instead of an alias in
each module that waits. Both callables keep their stdlib signatures.

Deadlines handed to ``asyncio.timeout`` / ``asyncio.timeout_at`` stay on
``loop.time()``: asyncio compares them against the loop's own clock, which is
not this seam.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

sleep: Callable[..., Coroutine[Any, Any, None]] = asyncio.sleep
monotonic: Callable[[], float] = time.monotonic

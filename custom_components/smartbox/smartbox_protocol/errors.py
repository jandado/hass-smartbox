"""Protocol-layer error hierarchy.

The names this layer uses, against the library hierarchy it replaces:

- ``AuthError``                 <- ``InvalidAuthError``
- ``TransportUnavailableError`` <- ``APIUnavailableError``
- ``TransportUnsupportedError`` <- ``WsUserUnsupportedError``

The transport policy from docs/api-contract.md §4.2: ONLY deterministic
handshake rejections (401/403/404/410 on a fresh-token capability probe) mean
"unsupported". Transient failures are retried and surface as
``TransportUnavailableError``.
"""

from __future__ import annotations


class SmartboxError(Exception):
    """Base class for every smartbox protocol error."""


class AuthError(SmartboxError):
    """Authentication rejected (invalid credentials, expired refresh)."""


class TransportUnavailableError(SmartboxError):
    """Transient transport failure; callers should retry with backoff."""


class TransportUnsupportedError(SmartboxError):
    """Endpoint deterministically rejected the capability probe."""


class ResellerNotExistError(SmartboxError):
    """Reseller api_name is not in the vendored table (and args are missing)."""

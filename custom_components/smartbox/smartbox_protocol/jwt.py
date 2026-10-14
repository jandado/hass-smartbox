"""Smartbox JWT claims: the credential facts the integration reads.

The token endpoint serves a JWT whose ``userId`` claim addresses the ws_user
socket and whose ``exp`` claim is the moment the vendor cuts a connection
opened with it (docs/api-contract.md §2.5, §4.1). Decoding is base64url over
the payload segment, so a token that does not decode is unusable credentials:
the errors are :class:`AuthError` (terminal for supervision, recovered through
the reauth flow) and never carry the token.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import AuthError

__all__ = ["jwt_expiry", "jwt_user_id"]

_JWT_SEGMENTS = 3


def _decode_claims(token: str) -> dict[str, Any] | None:
    """Decode a smartbox JWT's payload claims, or None when unreadable."""
    parts = token.split(".")
    if len(parts) != _JWT_SEGMENTS:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except ValueError:
        return None
    return claims if isinstance(claims, dict) else None


def jwt_expiry(token: str) -> float | None:
    """Return the ``exp`` claim of a smartbox JWT, or None when unreadable.

    Recognises the vendor's scheduled cut, which must not read as an outage
    (see ``SocketClosed.expected``). A missing or unusable ``exp`` is not an
    error: the close is then simply labelled unexpected.
    """
    claims = _decode_claims(token)
    exp = claims.get("exp") if claims is not None else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


def jwt_user_id(token: str) -> str:
    """Extract the ``userId`` claim from a Smartbox JWT.

    Pad-corrected base64url decode of the payload segment. A malformed or
    undecodable token, or a missing claim, raises :class:`AuthError`: the
    endpoint served credentials this client cannot use, so supervision stops
    and the reauth flow is the user-facing recovery. The message never
    contains the token.
    """
    parts = token.split(".")
    if len(parts) != _JWT_SEGMENTS:
        msg = "malformed smartbox JWT (expected 3 segments)"
        raise AuthError(msg)
    claims = _decode_claims(token)
    if claims is None:
        msg = "undecodable smartbox JWT payload"
        raise AuthError(msg)
    user_id = claims.get("userId")
    if not isinstance(user_id, str) or not user_id:
        msg = "smartbox JWT lacks a usable userId claim"
        raise AuthError(msg)
    return user_id

"""Auth/token lifecycle: password + refresh grants against the Smartbox API.

Wire facts (live-verified; canonical copies in docs/api-contract.md §2):

- ``POST {api_host}/client/token``, outside ``/api/v2`` (unlike every other
  API path; a wrong-path grant is a 404), with the reseller's Basic auth
  header passed VERBATIM plus ``x-serialid`` / ``x-referer``. Form body
  ``grant_type=password`` (fresh session) or ``grant_type=refresh_token``
  (steady state). The response is ``{access_token, expires_in, refresh_token,
  token_type}``: a 4 h JWT, with long-lived reusable refresh tokens rotated on
  every grant.
- A rejected refresh grant falls back to exactly ONE password grant; rejected
  credentials raise ``AuthError``. Transient failures retry with capped backoff
  inside a ~30 s deadline.
- Secrets hygiene: tokens, passwords and Basic creds never reach the logs, and
  ``ClientResponseError`` text stays out of messages and tracebacks because it
  renders the URL (``from None`` at the raise sites).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import datetime as dt
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientSession, ContentTypeError

from . import timing
from .errors import AuthError, SmartboxError, TransportUnavailableError
from .jwt import jwt_user_id
from .transport import TokenSource, WsCredentials

if TYPE_CHECKING:
    from typing import Any

_LOGGER = logging.getLogger(__name__)
__all__ = [
    "AuthConfig",
    "ManagedTokenSource",
    "TokenBundle",
    "TokenManager",
]

_BACKOFF_FACTOR = 1.0
_CALL_TIMEOUT = 30.0
_CLOSE_TIMEOUT = 5.0
_GRANT_ATTEMPTS = 4
_HTTP_BAD_REQUEST = 400
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500
_MIN_TOKEN_LIFETIME = 60.0
_GRANT_PATH = "/client/token"
# Sanity bound on a granted lifetime: the vendor serves a ~4 h JWT, so
# anything non-positive, non-finite or beyond a year is malformed. A
# non-positive value would make every read fire a fresh grant (the token is
# expired on arrival) and a huge one overflows the ``expires_at`` arithmetic.
_MAX_EXPIRES_IN_SECONDS = 86400 * 365


@dataclass(frozen=True, repr=False)
class AuthConfig:
    """Static credentials + endpoint identity for one account.

    ``basic_auth_creds`` is the reseller's base64 Basic-auth string, passed
    VERBATIM: the reseller identity travels only through the ``x-serialid`` and
    ``x-referer`` headers, and is never decoded client-side.
    """

    api_host: str
    basic_auth_creds: str
    serial_id: str
    username: str
    password: str
    referer: str | None = None

    def __post_init__(self) -> None:
        """Reject blank credentials early (fail at construction, not first use)."""
        for name in (
            "api_host",
            "basic_auth_creds",
            "serial_id",
            "username",
            "password",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                msg = f"AuthConfig.{name} must be a non-empty string"
                raise ValueError(msg)
        if self.referer is not None and not self.referer.strip():
            msg = "AuthConfig.referer must be a non-empty string when given"
            raise ValueError(msg)


@dataclass(frozen=True, repr=False)
class TokenBundle:
    """One granted token pair; ``expires_at`` derived from ``expires_in``."""

    access_token: str
    refresh_token: str
    expires_at: dt.datetime

    @classmethod
    def from_grant(
        cls,
        access_token: object,
        refresh_token: object,
        expires_in: object,
    ) -> TokenBundle:
        """Validate a token-endpoint payload and derive ``expires_at``."""
        if not (isinstance(access_token, str) and access_token):
            msg = "token grant payload lacks an access_token"
            raise SmartboxError(msg)
        if not (isinstance(refresh_token, str) and refresh_token):
            msg = "token grant payload lacks a refresh_token"
            raise SmartboxError(msg)
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            msg = "token grant payload lacks a numeric expires_in"
            raise SmartboxError(msg)
        # One comparison chain covers every unusable shape: nan and inf
        # both fail it (Python's json parses the non-standard Infinity /
        # NaN tokens), as do zero, negatives and absurdly large values.
        if not 0 < expires_in <= _MAX_EXPIRES_IN_SECONDS:
            msg = (
                "token grant payload carries an unusable expires_in "
                f"({type(expires_in).__name__}); expected 0 < seconds <= "
                f"{_MAX_EXPIRES_IN_SECONDS}"
            )
            raise SmartboxError(msg)
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires_in),
        )


class TokenManager:
    """Owns the current tokens and the (possibly owned) HTTP session.

    ``get_token`` is the single entry point for the REST client and the ws
    transport (via :class:`ManagedTokenSource`): the cached token is returned
    while >= ``min_lifetime_s`` remains, otherwise a refresh grant fires, or a
    password grant when no token is held. Concurrent callers never double-grant
    - the lock serializes the grant and the second caller reuses the result.
    """

    def __init__(
        self,
        config: AuthConfig,
        session: ClientSession | None = None,
    ) -> None:
        """Inject an existing session, or lazily create (and own) one."""
        self._config = config
        self._session = session
        self._owns_session = False  # only set when lazily created
        self._tokens: TokenBundle | None = None
        self._force_refresh = False
        self._lock = asyncio.Lock()

    async def get_token(
        self, min_lifetime_s: float = _MIN_TOKEN_LIFETIME
    ) -> TokenBundle:
        """Return the current token, refreshing or re-granting as needed.

        The LOCK is acquired FIRST: the ~30 s grant budget starts once it is
        held, so a caller queued behind a concurrent grant still gets a full
        budget, while the inner grant deadlines keep a hung holder from stalling
        it forever. Raises ``AuthError`` when the credentials are rejected.
        """
        try:
            async with self._lock:
                async with asyncio.timeout(_CALL_TIMEOUT):
                    return await self._get_token_locked(min_lifetime_s)
        except TimeoutError as e:
            msg = f"smartbox token acquisition did not complete within {_CALL_TIMEOUT}s"
            raise TransportUnavailableError(msg) from e

    @property
    def token_expiry(self) -> dt.datetime | None:
        """Expiry of the CACHED token bundle, None while no token is held.

        Read-only surface for diagnostics: it shows when the token ran out
        without leaking the token.
        """
        return self._tokens.expires_at if self._tokens is not None else None

    async def invalidate(self, rejected_token: str | None = None) -> None:
        """Mark the current access token rejected.

        The next :meth:`get_token` fires a real refresh grant. ``rejected_token``
        makes this a no-op when the token was already replaced, so concurrent
        401s cannot trigger a refresh storm.
        """
        if rejected_token is not None and (
            self._tokens is None or self._tokens.access_token != rejected_token
        ):
            return
        if self._tokens is not None:
            self._force_refresh = True

    async def reauthenticate(self) -> TokenBundle:
        """Clear the tokens and run one fresh password grant.

        The recovery path for a dead refresh token. Raises ``AuthError`` when
        the password credentials are rejected.
        """
        try:
            async with self._lock:
                async with asyncio.timeout(_CALL_TIMEOUT):
                    self._tokens = None
                    self._force_refresh = False
                    return await self._grant_with_retry(
                        self._password_body(), self._deadline()
                    )
        except TimeoutError as e:
            msg = f"smartbox re-authentication did not complete within {_CALL_TIMEOUT}s"
            raise TransportUnavailableError(msg) from e

    def _ensure_session(self) -> ClientSession:
        """Return the injected session, or lazily create (and own) one."""
        if self._session is None:
            self._session = ClientSession()
            self._owns_session = True
        return self._session

    @property
    def session(self) -> ClientSession:
        """The underlying HTTP session (lazily created when not injected)."""
        return self._ensure_session()

    async def close(self) -> None:
        """Close the OWNED session, if any; injected sessions stay open."""
        if self._owns_session and self._session is not None:
            session, self._session = self._session, None
            self._owns_session = False
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(session.close(), _CLOSE_TIMEOUT)

    # --- grant machinery ---

    async def _get_token_locked(self, min_lifetime_s: float) -> TokenBundle:
        tokens = self._tokens
        if (
            tokens is not None
            and not self._force_refresh
            and self._remaining_s(tokens) >= min_lifetime_s
        ):
            return tokens
        if tokens is None:
            return await self._grant_with_retry(self._password_body(), self._deadline())
        try:
            return await self._grant_with_retry(
                {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token},
                self._deadline(),
            )
        except AuthError:
            # Dead refresh token: exactly ONE password fallback before
            # AuthError may reach the caller.
            _LOGGER.warning(
                "smartbox refresh token rejected; retrying once with password grant"
            )
            self._tokens = None
            self._force_refresh = False
            return await self._grant_with_retry(self._password_body(), self._deadline())

    async def _grant_with_retry(
        self, body: dict[str, str], deadline: float
    ) -> TokenBundle:
        """Run one grant with capped backoff on transient failures."""
        loop = asyncio.get_running_loop()
        for attempt in range(_GRANT_ATTEMPTS):
            try:
                return await self._grant(body, deadline)
            except TransportUnavailableError:
                backoff = _BACKOFF_FACTOR * (2**attempt)
                if attempt == _GRANT_ATTEMPTS - 1 or loop.time() + backoff >= deadline:
                    raise
                await timing.sleep(backoff)
        raise AssertionError  # pragma: no cover - loop always returns/raises

    async def _grant(self, body: dict[str, str], deadline: float) -> TokenBundle:
        """POST one token grant; map failures per the error contract.

        429 -> ``TransportUnavailableError``: rate limiting is transient, never
        a credential rejection, and retrying keeps the grant ladder from
        amplifying the limit. Other 4xx -> ``AuthError``, the only path that may
        trigger the refresh -> password fallback. 5xx and network failures ->
        ``TransportUnavailableError``. A 200 whose body is undecodable, non-JSON
        or otherwise unusable is a malformed success (``SmartboxError``): never
        retried, never read as a credential rejection.
        """
        headers = {
            "Authorization": f"Basic {self._config.basic_auth_creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-serialid": self._config.serial_id,
        }
        if self._config.referer is not None:
            headers["x-referer"] = self._config.referer
        url = f"{self._config.api_host.rstrip('/')}{_GRANT_PATH}"
        grant_type = body["grant_type"]
        _LOGGER.debug("smartbox token grant (%s)", grant_type)
        try:
            async with asyncio.timeout_at(deadline):
                async with self._ensure_session().post(
                    url, headers=headers, data=body
                ) as resp:
                    status = resp.status
                    if status >= _HTTP_BAD_REQUEST:
                        if status == _HTTP_TOO_MANY_REQUESTS:
                            # Rate limiting is transient: retry via the
                            # grant ladder, never the password fallback
                            # (fallback amplifies the limit).
                            msg = f"smartbox token endpoint rate-limited (status={status})"
                            raise TransportUnavailableError(msg) from None
                        if status < _HTTP_SERVER_ERROR:
                            msg = f"smartbox rejected the {grant_type} grant (status={status})"
                            raise AuthError(msg) from None
                        msg = f"smartbox token endpoint unavailable (status={status})"
                        raise TransportUnavailableError(msg) from None
                    try:
                        payload = await resp.json()
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        # A valid content type with an undecodable body
                        # (malformed JSON, or bytes that are not decodable
                        # text) is a malformed success: mapping it to
                        # AuthError would show "credentials rejected" for a
                        # server-side glitch (same mapping as
                        # client.py::_decode).
                        msg = (
                            "smartbox token endpoint returned an undecodable "
                            f"{grant_type} body (status={resp.status}, "
                            f"content_type={resp.content_type}, "
                            f"length={resp.content_length})"
                        )
                        raise SmartboxError(msg) from e
                    except ContentTypeError:
                        # 200 with a non-JSON body (captive portal, proxy,
                        # reseller gateway): also a malformed success. The
                        # chain is cut because ContentTypeError renders the
                        # request URL in its traceback.
                        msg = (
                            "smartbox token endpoint returned a non-JSON "
                            f"{grant_type} body (status={resp.status}, "
                            f"content_type={resp.content_type})"
                        )
                        raise SmartboxError(msg) from None
        except (TimeoutError, ClientError, OSError) as e:
            msg = f"smartbox token endpoint unreachable ({type(e).__name__})"
            raise TransportUnavailableError(msg) from e
        return self._decode_grant(payload, grant_type)

    def _decode_grant(
        self,
        payload: dict[str, Any],
        grant_type: str,
    ) -> TokenBundle:
        """Validate + store a granted bundle (rotation is implicit)."""
        try:
            bundle = TokenBundle.from_grant(
                payload["access_token"],
                payload["refresh_token"],
                payload["expires_in"],
            )
        except (KeyError, TypeError) as e:
            msg = f"token endpoint returned a malformed {grant_type} response"
            raise SmartboxError(msg) from e
        self._tokens = bundle
        self._force_refresh = False
        _LOGGER.debug("smartbox %s grant ok", grant_type)
        return bundle

    def _password_body(self) -> dict[str, str]:
        return {
            "grant_type": "password",
            "username": self._config.username,
            "password": self._config.password,
        }

    @staticmethod
    def _deadline() -> float:
        return asyncio.get_running_loop().time() + _CALL_TIMEOUT

    @staticmethod
    def _remaining_s(tokens: TokenBundle) -> float:
        remaining = tokens.expires_at - dt.datetime.now(dt.UTC)
        return remaining.total_seconds()


class ManagedTokenSource(TokenSource):
    """Adapter: feeds the ws transport from a :class:`TokenManager`."""

    def __init__(self, manager: TokenManager) -> None:
        """Wrap one manager; acquire() and invalidate() delegate to it."""
        self._manager = manager

    @property
    def manager(self) -> TokenManager:
        """The token manager this source delegates to (read-only)."""
        return self._manager

    async def acquire(self) -> WsCredentials:
        """Fresh token for the next connect (user_id from the JWT claim)."""
        bundle = await self._manager.get_token()
        return WsCredentials(
            token=bundle.access_token,
            user_id=jwt_user_id(bundle.access_token),
        )

    async def invalidate(self, rejected: WsCredentials) -> None:
        """Mark the rejected token unusable; force a refresh next acquire."""
        await self._manager.invalidate(rejected.token)

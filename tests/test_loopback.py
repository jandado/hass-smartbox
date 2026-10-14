"""Loopback tests: the REAL aiohttp behaviors behind the duck-typed seams.

Runs the real ``TokenManager`` / ``SmartboxClient`` / ``WsUserTransport``
against a real local aiohttp server (``TestServer``), pinning the wire
behaviors the ``FakeSession``/``FakeWS`` fakes assume: the ``ws_connect``
context handshake, close-while-receiving (abnormal 1006),
``ClientResponse.json``/``content_length`` semantics, and the
``TimeoutError``/``ClientError`` shape of real timeouts. Only the sleeps
go through the shared ``timing.sleep`` seam - no real waits.
"""

from __future__ import annotations

import asyncio
from collections import deque
import time
from typing import Any

import aiohttp
from aiohttp import test_utils, web
import pytest

from custom_components.smartbox.smartbox_protocol import client as client_mod, timing
from custom_components.smartbox.smartbox_protocol.auth import AuthConfig, TokenManager
from custom_components.smartbox.smartbox_protocol.client import NodeRef, SmartboxClient
from custom_components.smartbox.smartbox_protocol.errors import (
    SmartboxError,
    TransportUnavailableError,
)
from custom_components.smartbox.smartbox_protocol.events import (
    Snapshot,
    SocketClosed,
    Update,
)
from custom_components.smartbox.smartbox_protocol.writes import WriteApi
from custom_components.smartbox.smartbox_protocol.ws_user import WsUserTransport
from tests.fakes import FakeTokenSource, SleepController, make_jwt
from tests.test_utils import until

BASIC_CREDS = "cmVzZWxsZXJfc2VjcmV0"
SERIAL_ID = "1234"
EMAIL = "user@example.test"
PASSWORD = "hunter2"

JWT1 = make_jwt({"userId": "u-loop", "exp": 9999999999})
JWT2 = make_jwt({"userId": "u-loop", "exp": 9999999999})

GRANT1 = {"access_token": JWT1, "refresh_token": "rt1", "expires_in": 14400}
GRANT2 = {"access_token": JWT2, "refresh_token": "rt2", "expires_in": 14400}

DEV_PAYLOAD = {"devs": [{"dev_id": "dev1", "name": "Heater", "serial_id": 42}]}
HOMES = [{"home_id": "h1", "devs": [{"dev_id": "dev1", "devData": {"mtemp": "190"}}]}]
UPDATE_FRAME = {
    "event": "update",
    "devid": "dev1",
    "data": {"path": "/htr/2/status", "body": {"mtemp": "200"}},
}
NODE = NodeRef(node_type="htr", addr="2")

# script-entry sentinel: a success response with an EMPTY body
_EMPTY = object()

# aiohttp behavior pin: ServerTimeoutError (real sock_read timeouts)
# must stay inside the (TimeoutError, ClientError, OSError) catch.


def test_server_timeout_error_shape():
    assert issubclass(aiohttp.ServerTimeoutError, TimeoutError)
    assert issubclass(aiohttp.ServerTimeoutError, aiohttp.ClientError)


class LoopbackAPI:
    """Real local aiohttp server standing in for the Smartbox cloud.

    Records what the real requests carried (grant bodies, auth headers,
    ws tokens); ``enqueue_devs``/``enqueue_status`` script per-endpoint
    answers as ``(status, payload_or__EMPTY, delay)`` tuples.
    """

    def __init__(self) -> None:
        self.host = ""
        self.grants: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.ws_connections: list[dict[str, str]] = []
        self.release = asyncio.Event()
        # set by the test once the client observed the frames: the ws
        # handler waits on it before aborting the TCP connection
        self.ws_frames_delivered = asyncio.Event()
        self._devs_script: deque[tuple[int, Any, float]] = deque()
        self._status_script: deque[tuple[int, Any, float]] = deque()

    def enqueue_devs(self, *entries: tuple[int, Any, float]) -> None:
        self._devs_script.extend(entries)

    def enqueue_status(self, *entries: tuple[int, Any, float]) -> None:
        self._status_script.extend(entries)

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/client/token", self._token)
        app.router.add_get("/api/v2/devs", self._devs)
        app.router.add_post(
            "/api/v2/devs/{dev_id}/{node_type}/{addr}/status", self._status,
        )
        app.router.add_get("/api/v2/ws_user", self._ws_user)
        return app

    def _record(self, request: web.Request, **extra: object) -> dict[str, Any]:
        record: dict[str, Any] = {
            "path": request.path,
            "auth": request.headers.get("Authorization"),
            **extra,
        }
        self.requests.append(record)
        return record

    async def _serve(
        self,
        script: deque[tuple[int, Any, float]],
        default: object,
    ) -> web.Response:
        status, payload, delay = script.popleft() if script else (200, default, 0.0)
        if delay:
            await asyncio.sleep(delay)
        if payload is _EMPTY:
            return web.Response(status=status)
        if isinstance(payload, web.Response):
            return payload
        return web.json_response(payload, status=status)

    async def _token(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.grants.append(
            {
                "grant_type": form["grant_type"],
                "email": form.get("username"),
                "password": form.get("password"),
                "refresh_token": form.get("refresh_token"),
                "basic": request.headers.get("Authorization"),
                "serial_id": request.headers.get("x-serialid"),
                "username": form.get("username"),
            },
        )
        if form["grant_type"] == "password":
            return web.json_response(GRANT1)
        if form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt1":
            return web.json_response(GRANT2)
        return web.json_response({"error": "bad grant"}, status=400)

    async def _devs(self, request: web.Request) -> web.Response:
        self._record(request)
        return await self._serve(self._devs_script, DEV_PAYLOAD)

    async def _status(self, request: web.Request) -> web.Response:
        self._record(request, body=await request.json())
        return await self._serve(self._status_script, {})

    async def _ws_user(self, request: web.Request) -> web.StreamResponse:
        self.ws_connections.append(
            {"token": request.query["token"], "user_id": request.query["user_id"]},
        )
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive()  # the client's all_data frame on open
        if len(self.ws_connections) == 1:
            await ws.send_json({"event": "all_data", "data": HOMES})
            await ws.send_json(UPDATE_FRAME)
            # event-driven: abort only after the client observed the
            # frames (a fixed sleep races under load)
            await self.ws_frames_delivered.wait()
            if request.transport is not None:
                # drop the TCP connection WITHOUT a ws close frame:
                # the client must see this as an abnormal close (1006)
                request.transport.abort()
        await self.release.wait()
        return ws


@pytest.fixture
async def api(socket_enabled):
    state = LoopbackAPI()
    server = test_utils.TestServer(state.make_app())
    await server.start_server()
    state.host = str(server.make_url("")).rstrip("/")
    try:
        yield state
    finally:
        state.release.set()
        await server.close()


@pytest.fixture
def sleeps(monkeypatch):
    controller = SleepController()
    monkeypatch.setattr(timing, "sleep", controller)
    monkeypatch.setattr(timing, "sleep", controller)
    monkeypatch.setattr(timing, "sleep", controller)
    return controller


def make_config(api: LoopbackAPI) -> AuthConfig:
    return AuthConfig(
        api_host=api.host,
        basic_auth_creds=BASIC_CREDS,
        serial_id=SERIAL_ID,
        username=EMAIL,
        password=PASSWORD,
    )


# --- REST: token grants ---


async def test_password_grant_then_authorized_get(api):
    client = SmartboxClient(make_config(api))
    try:
        assert await client.get_devices() == DEV_PAYLOAD
    finally:
        await client.close()
    grant = api.grants[0]
    assert grant["grant_type"] == "password"
    assert grant["username"] == EMAIL
    assert grant["password"] == PASSWORD
    assert grant["basic"] == f"Basic {BASIC_CREDS}"
    assert grant["serial_id"] == SERIAL_ID
    assert api.requests[-1]["auth"] == f"Bearer {JWT1}"
    assert api.requests[-1]["path"] == "/api/v2/devs"


async def test_refresh_grant_after_invalidate(api):
    manager = TokenManager(make_config(api))
    try:
        bundle1 = await manager.get_token()
        await manager.invalidate(bundle1.access_token)
        bundle2 = await manager.get_token()
    finally:
        await manager.close()
    assert [g["grant_type"] for g in api.grants] == ["password", "refresh_token"]
    assert api.grants[1]["refresh_token"] == "rt1"
    assert bundle2.access_token == JWT2


# --- REST: 401 -> invalidate + re-auth + resend (body preserved) ---


async def test_get_401_resends_after_refresh_grant(api):
    api.enqueue_devs((401, _EMPTY, 0.0))
    client = SmartboxClient(make_config(api))
    try:
        assert await client.get_devices() == DEV_PAYLOAD
    finally:
        await client.close()
    assert [g["grant_type"] for g in api.grants] == ["password", "refresh_token"]
    assert [r["auth"] for r in api.requests] == [
        f"Bearer {JWT1}",
        f"Bearer {JWT2}",
    ]


async def test_write_post_401_resend_preserves_the_body(api):
    api.enqueue_status((401, _EMPTY, 0.0), (200, {}, 0.0))
    client = SmartboxClient(make_config(api))
    try:
        await WriteApi(client).set_mode("dev1", NODE, "auto")
    finally:
        await client.close()
    assert [g["grant_type"] for g in api.grants] == ["password", "refresh_token"]
    posts = [r for r in api.requests if r["path"].endswith("/status")]
    assert len(posts) == 2
    assert posts[0]["body"] == {"mode": "auto"}
    assert posts[1]["body"] == {"mode": "auto"}
    assert posts[1]["auth"] == f"Bearer {JWT2}"


# --- REST: 5xx retry / single-attempt writes / decode / deadline ---


async def test_get_5xx_retried_then_recovers(api, sleeps):
    api.enqueue_devs((500, _EMPTY, 0.0), (503, _EMPTY, 0.0))
    client = SmartboxClient(make_config(api))
    try:
        assert await client.get_devices() == DEV_PAYLOAD
    finally:
        await client.close()
    assert sleeps.delays == [1.0, 2.0]
    assert len(api.requests) == 3


async def test_get_5xx_exhausted_raises_transport_unavailable(api, sleeps):
    api.enqueue_devs((500, _EMPTY, 0.0), (500, _EMPTY, 0.0), (500, _EMPTY, 0.0))
    client = SmartboxClient(make_config(api))
    try:
        with pytest.raises(TransportUnavailableError):
            await client.get_devices()
    finally:
        await client.close()
    assert sleeps.delays == [1.0, 2.0]
    assert len(api.requests) == 3


async def test_write_post_5xx_is_a_single_attempt(api):
    api.enqueue_status((503, _EMPTY, 0.0))
    client = SmartboxClient(make_config(api))
    try:
        with pytest.raises(TransportUnavailableError):
            await WriteApi(client).set_mode("dev1", NODE, "auto")
    finally:
        await client.close()
    posts = [r for r in api.requests if r["path"].endswith("/status")]
    assert len(posts) == 1


async def test_empty_body_and_204_decode_to_empty_dict(api):
    api.enqueue_devs((200, _EMPTY, 0.0), (204, _EMPTY, 0.0))
    client = SmartboxClient(make_config(api))
    try:
        assert await client.get_devices() == {}
        assert await client.get_devices() == {}
    finally:
        await client.close()


async def test_2xx_undecodable_json_raises_smartbox_error(api):
    """Real aiohttp: HTML bytes under a JSON content type on a 200.

    The decode failure enters the error contract (SmartboxError), with
    the message bounded to status/type/length - never the body.
    """
    html = web.Response(text="<html>gateway error</html>", content_type="application/json")
    api.enqueue_devs((200, html, 0.0))
    client = SmartboxClient(make_config(api))
    try:
        with pytest.raises(SmartboxError, match="undecodable JSON body") as excinfo:
            await client.get_devices()
    finally:
        await client.close()
    assert "gateway error" not in str(excinfo.value)  # body never in messages


async def test_deadline_bounds_a_hung_get(api, monkeypatch):
    monkeypatch.setattr(client_mod, "_CALL_TIMEOUT", 0.2)
    api.enqueue_devs((200, DEV_PAYLOAD, 5.0))
    client = SmartboxClient(make_config(api))
    try:
        started = time.monotonic()
        with pytest.raises(TransportUnavailableError):
            await client.get_devices()
    finally:
        await client.close()
    # Wall-clock pin (suite-wide pytest-timeout cap is 120 s - ample
    # headroom; this local bound is what catches a hung get).
    assert time.monotonic() - started < 2.0


async def test_real_sock_read_timeout_maps_to_transport_unavailable(api, sleeps):
    """Real aiohttp ``ServerTimeoutError`` (sock_read) -> mapped error.

    Every attempt must hang: a GET retry after the first timeout would
    otherwise hit the scripted success.
    """
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_read=0.1),
    )
    api.enqueue_devs(*[(200, DEV_PAYLOAD, 5.0)] * client_mod._RETRY_ATTEMPTS)
    client = SmartboxClient(make_config(api), session=session)
    try:
        with pytest.raises(TransportUnavailableError):
            await client.get_devices()
    finally:
        await session.close()


# --- ws_user: frames + abnormal close + fresh-token reconnect ---


async def test_ws_frames_then_abnormal_close_reconnects_with_fresh_token(
    api,
    sleeps,
):
    transport = WsUserTransport(FakeTokenSource(), api.host)
    events: list[Any] = []
    transport.subscribe(events.append)
    run_task = asyncio.create_task(transport.run())
    try:
        await until(lambda: len(api.ws_connections) >= 1)
        await until(lambda: len(events) >= 2)  # Snapshot + Update observed
        api.ws_frames_delivered.set()  # server aborts -> abnormal close
        assert api.ws_connections[0] == {"token": "t1", "user_id": "u1"}
        await until(lambda: len(events) >= 3)
        assert [type(event) for event in events] == [Snapshot, Update, SocketClosed]
        assert events[0].payload == HOMES
        assert events[1] == Update(
            dev_id="dev1", path="/htr/2/status", body={"mtemp": "200"},
        )
        assert events[2].code == 1006  # abnormal: no close frame on the wire
        await until(lambda: len(api.ws_connections) >= 2)
        assert api.ws_connections[1] == {"token": "t2", "user_id": "u2"}
    finally:
        await transport.close()
        await asyncio.wait_for(run_task, 2.0)


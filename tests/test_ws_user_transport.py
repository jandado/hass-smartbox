"""Tests for the real ws_user transport.

Plain pytest (no HA harness). All sleeps are intercepted via the
``timing.sleep`` seam (instant or gated), so no test ever really
waits for backoff or the debounce window.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import json
import logging
import time
import traceback

import aiohttp
from multidict import CIMultiDict
import pytest
from yarl import URL

from custom_components.smartbox.smartbox_protocol import timing, ws_user as wu
from custom_components.smartbox.smartbox_protocol.auth import (
    AuthConfig,
    ManagedTokenSource,
    TokenManager,
)
from custom_components.smartbox.smartbox_protocol.errors import (
    AuthError,
    TransportUnsupportedError,
)
from custom_components.smartbox.smartbox_protocol.events import (
    Snapshot,
    SocketClosed,
    Update,
)
from custom_components.smartbox.smartbox_protocol.transport import Transport
from custom_components.smartbox.smartbox_protocol.ws_user import WsCredentials
from tests.fakes import (
    FakeResponse,
    FakeSession as FakeRestSession,
    FakeTokenSource,
    HandshakeRejected,
    make_jwt,
)
from tests.test_utils import until

API_HOST = "https://api.example.test"

ALL_DATA = '{"event": "all_data"}'


def text(frame: dict) -> tuple[str, str]:
    """Script entry: one TEXT frame carrying ``frame`` as JSON."""
    return ("text", json.dumps(frame))


def close_entry(code: int) -> tuple[str, int]:
    """Script entry: server closes with ``code``."""
    return ("close", code)


def handshake_error(status: int) -> HandshakeRejected:
    """Realistic handshake failure.

    The caught error's text carries the actual token-bearing ws_user
    URL, so the no-token assertions are non-vacuous.
    """
    return HandshakeRejected(status)


class SleepController:
    """Replacement for the ``timing.sleep`` seam.

    Instant mode records the requested delay and returns immediately;
    gated mode also blocks each caller on a shared gate so tests can
    freeze the debounce window deterministically.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.gated = False
        self.gate_entries = 0
        self._gate = asyncio.Event()

    async def __call__(self, delay: float = 0) -> None:
        self.delays.append(delay)
        if not self.gated:
            return
        self.gate_entries += 1
        await self._gate.wait()

    def release(self) -> None:
        self._gate.set()


class FakeWS:
    """Scripted aiohttp-like ws connection (receive/send/close only)."""

    def __init__(self, script: list | None = None) -> None:
        self.script = deque(script or [])
        self.sent: list[str] = []
        self._close_requested = asyncio.Event()

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def receive(self) -> aiohttp.WSMessage:
        if self.script:
            entry = self.script.popleft()
            if isinstance(entry, Exception):
                raise entry
            kind, value = entry
            if kind == "text":
                return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, value, None)
            if kind == "binary":
                return aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, value, None)
            self._close_requested.set()
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, value, None)
        # parks the pump until a scripted close or transport close() ends us
        await self._close_requested.wait()
        return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)

    async def close(self) -> None:
        self._close_requested.set()


class ChattyWS(FakeWS):
    """A socket that keeps delivering update frames but never a snapshot."""

    def __init__(self, interval: float = 0.01) -> None:
        super().__init__()
        self.interval = interval
        self.updates = 0

    async def receive(self) -> aiohttp.WSMessage:
        await asyncio.sleep(self.interval)
        self.updates += 1
        return aiohttp.WSMessage(
            aiohttp.WSMsgType.TEXT,
            json.dumps(
                {
                    "event": "update",
                    "devid": "d1",
                    "data": {"path": "/htr/1/status", "body": {"mtemp": "20.0"}},
                }
            ),
            None,
        )


class FakeSession:
    """Duck-typed ClientSession: scripted connections, recorded URLs."""

    def __init__(self, hang_first: asyncio.Event | None = None) -> None:
        self.connections: deque = deque()
        self.urls: list[str] = []
        self.heartbeats: list[float | None] = []
        self.last_ws: FakeWS | None = None
        self._hang_first = hang_first
        self._hang_used = False

    def enqueue(self, item) -> None:
        """Queue the next connection: a FakeWS or an Exception to raise."""
        self.connections.append(item)

    def ws_connect(self, url: str, heartbeat=None) -> _ConnectCtx:
        self.urls.append(url)
        self.heartbeats.append(heartbeat)
        return _ConnectCtx(self, url)


class _ConnectCtx:
    """Duck-typed ws_connect context manager."""

    def __init__(self, session: FakeSession, url: str) -> None:
        self._session = session
        self._url = url

    async def __aenter__(self):
        if self._session._hang_first is not None and not self._session._hang_used:
            self._session._hang_used = True
            await self._session._hang_first.wait()
        item = (
            FakeWS() if not self._session.connections else self._session.connections.popleft()
        )
        if isinstance(item, HandshakeRejected):
            # raise the REALISTIC error bound to the connect URL (its
            # __str__ carries the token-bearing URL)
            url = URL(self._url)
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(url, "GET", CIMultiDict(), url),
                history=(),
                status=item.status,
                message="handshake rejected",
            )
        if isinstance(item, Exception):
            raise item
        self._session.last_ws = item
        return item

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False  # the transport closes the ws itself


def make_transport(session, token_source=None, **kwargs):
    ts = token_source or FakeTokenSource()
    transport = wu.WsUserTransport(ts, API_HOST, session=session, **kwargs)
    events: list = []

    async def handler(event) -> None:
        events.append(event)

    transport.subscribe(handler)
    return transport, ts, events


@pytest.fixture
def sleeps(monkeypatch):
    controller = SleepController()
    monkeypatch.setattr(timing, "sleep", controller)
    return controller


@pytest.fixture(autouse=True)
def snapshot_watchdog_off(monkeypatch):
    """Park the connect watchdog unless a test turns it on.

    Most tests here script frames or park the socket without an all_data
    reply; the watchdog has its own test.
    """
    monkeypatch.setattr(wu, "_SNAPSHOT_TIMEOUT", 3600.0)


@pytest.fixture
def session():
    return FakeSession()


# --- frame handling ---


async def test_all_data_sent_on_open(sleeps, session):
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: bool(session.urls))
    assert session.urls[0] == (
        "wss://api.example.test:443/api/v2/ws_user?token=t1&user_id=u1"
    )
    assert session.heartbeats[0] == 60.0
    assert session.last_ws.sent == [ALL_DATA]
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_snapshot_dispatch_unwraps_data_key(sleeps, session):
    homes = [
        {"home_id": "h1", "devs": [{"dev_id": "d1", "devData": {"mtemp": "190"}}]},
        {"home_id": "h2", "devs": []},
    ]
    session.enqueue(FakeWS([text({"event": "all_data", "data": homes})]))
    transport, _ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(events) >= 1)
    assert isinstance(events[0], Snapshot)
    assert list(events[0].payload) == homes
    assert events[0].payload[0]["devs"][0]["devData"]["mtemp"] == "190"
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_update_dispatch(sleeps, session):
    frame = {
        "event": "update",
        "devid": "dev1",
        "data": {"path": "/htr/1/status", "body": {"mode": "auto"}},
    }
    session.enqueue(FakeWS([text(frame)]))
    transport, _ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(events) >= 1)
    assert events[0] == Update(dev_id="dev1", path="/htr/1/status", body={"mode": "auto"})
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_update_forged_path_single_line_logs(sleeps, session, caplog):
    # a server-controlled path/dev_id with embedded newlines must render
    # escaped (%r), never forge multi-line log entries
    frame = {
        "event": "update",
        "devid": "dev1\nFORGED WARNING",
        "data": {"path": "/htr/1/status\nFAKE: root access", "body": {"mode": "auto"}},
    }
    session.enqueue(FakeWS([text(frame)]))
    transport, _ts, events = make_transport(session)
    with caplog.at_level(
        logging.DEBUG, logger="custom_components.smartbox.smartbox_protocol.ws_user"
    ):
        run_task = asyncio.create_task(transport.run())
        await until(lambda: len(events) >= 1)
    assert events[0].path == "/htr/1/status\nFAKE: root access"
    multi = [rec.getMessage() for rec in caplog.records if "\n" in rec.getMessage()]
    assert not multi
    # the details are still there (escaped)
    assert any("FAKE" in rec.getMessage() for rec in caplog.records)
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_should_sync_debounce_sends_exactly_one_all_data(sleeps, session):
    sleeps.gated = True
    session.enqueue(
        FakeWS([text({"event": "should_sync"}), text({"event": "should_sync"})]),
    )
    transport, _ts, _events = make_transport(session, should_sync_debounce=1.0)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: session.last_ws is not None and session.last_ws.sent == [ALL_DATA])
    ws = session.last_ws
    await until(lambda: sleeps.gate_entries >= 1)  # the (replaced) debounce is sleeping
    assert not ws.script  # both should_sync frames were consumed
    assert ws.sent == [ALL_DATA]  # nothing sent while the gate is shut
    sleeps.release()
    await until(lambda: len(ws.sent) == 2)
    assert ws.sent == [ALL_DATA, ALL_DATA]  # exactly ONE debounced send
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_malformed_frames_skipped_pump_alive(sleeps, session):
    good_update = {
        "event": "update",
        "devid": "d1",
        "data": {"path": "/p", "body": {"x": 1}},
    }
    script = [
        ("text", "not json {"),
        ("text", json.dumps("a string, not an object")),
        ("text", json.dumps({"event": "all_data"})),  # missing data
        ("text", json.dumps({"event": "all_data", "data": "nope"})),
        ("text", json.dumps({"event": "update", "devid": "d1", "data": {}})),
        ("text", json.dumps({"event": "update", "data": {"path": "/p", "body": {}}})),
        ("text", json.dumps({"event": "update", "devid": 42, "data": {"path": "/p", "body": {}}})),
        ("text", json.dumps({"event": "unheard_of"})),  # unknown event ignored
        ("binary", b"\x00\x01"),  # non-TEXT frame skipped
        text(good_update),
    ]
    session.enqueue(FakeWS(script))
    transport, _ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(events) >= 1)
    assert events == [Update(dev_id="d1", path="/p", body={"x": 1})]
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_resync_send_failure_is_logged_not_fatal(sleeps, session, caplog):
    """A debounced resync whose send fails is logged, never fatal.

    The socket can die between the ``should_sync`` frame and the resync
    send; that must not escape the resync task and kill the pump.
    """
    ws = FakeWS(
        [text({"event": "should_sync"}), text({"event": "all_data", "data": []})]
    )
    session.enqueue(ws)
    transport, _ts, events = make_transport(session)
    sends = {"n": 0}

    async def failing_send(_data: str) -> None:
        sends["n"] += 1
        if sends["n"] > 1:  # the connect-time all_data went out; resync fails
            msg = "socket gone"
            raise aiohttp.ClientError(msg)

    ws.send_str = failing_send
    with caplog.at_level(logging.WARNING, logger=wu.__name__):
        run_task = asyncio.create_task(transport.run())
        await until(
            lambda: any("resync send failed" in r.message for r in caplog.records)
        )
    # the pump survived: the frame after the failed resync still arrived
    assert events == [Snapshot(payload=[])]
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_handler_exception_isolated(sleeps, session):
    session.enqueue(
        FakeWS(
            [
                text({"event": "all_data", "data": [{"h": 1}]}),
                text({"event": "update", "devid": "d", "data": {"path": "/p", "body": {}}}),
            ],
        ),
    )
    transport, _ts, events = make_transport(session)

    async def bad_handler(event) -> None:
        events.append(event)
        if len(events) == 1:
            msg = "boom"
            raise RuntimeError(msg)

    transport.subscribe(bad_handler)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(events) >= 2)
    assert len(events) == 2  # second delivery still reached
    await transport.close()
    await asyncio.wait_for(run_task, 2)


# --- reconnect / backoff ---


async def test_reconnect_after_1006_with_fresh_token(sleeps, session):
    session.enqueue(
        FakeWS(
            [
                text({"event": "update", "devid": "d", "data": {"path": "/p", "body": {}}}),
                close_entry(1006),
            ],
        ),
    )
    transport, ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(events) >= 2)
    assert isinstance(events[1], SocketClosed)
    assert events[1].code == 1006
    await until(lambda: len(session.urls) >= 2)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert "token=t2&user_id=u2" in session.urls[1]
    assert ts.invalidated == []  # 1006 is not an auth rejection


async def test_backoff_progression_and_jitter_bounds(sleeps, session):
    for _ in range(10):
        session.enqueue(aiohttp.ClientError("connect refused"))
    transport, ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: ts.calls >= 11)  # exhausted, refreshed, retried
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    backoffs = sleeps.delays[:10]
    assert len(backoffs) == 10
    for n, delay in enumerate(backoffs):
        base = 2.0**n  # backoff_factor 1.0
        assert min(1.0 * base, 30.0) <= delay <= min(1.2 * base, 30.0) + 1e-9
    assert sleeps.delays[10] == pytest.approx(1.0)  # between-cycle pause


async def test_close_interrupts_an_in_cycle_backoff(session, monkeypatch):
    """close() must not wait out a connect-backoff sleep.

    The in-cycle retry sleep races the stop event (as the between-cycles
    pause does): without that, a close landing inside the backoff would sit
    out the whole capped delay with the run task still alive.
    """
    parking = asyncio.Event()

    async def parked_sleep(_delay=0):
        await parking.wait()

    monkeypatch.setattr(timing, "sleep", parked_sleep)
    session.enqueue(aiohttp.ClientError("dns"))
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: session.urls)  # the first attempt failed; backoff starts
    await transport.close()
    await asyncio.wait_for(run_task, 1)
    parking.set()


async def test_attempt_exhaustion_refreshes_auth_and_retries(sleeps, session):
    for _ in range(10):
        session.enqueue(aiohttp.ClientError("dns"))
    transport, ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(session.urls) >= 11)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert ts.calls == 11
    assert "token=t11&user_id=u11" in session.urls[10]
    # a cycle that never served a connection still reports the socket ended:
    # the engine's feed state is driven by these events alone, so a transport
    # stuck in failed connects must not leave the feed marked up
    assert [e for e in events if isinstance(e, wu.SocketClosed)]
    assert not any(getattr(e, "expected", True) for e in events if isinstance(e, wu.SocketClosed))


async def test_handshake_timeout_retried_not_unsupported(sleeps, session):
    hang = asyncio.Event()
    hanging_session = FakeSession(hang_first=hang)
    transport, ts, _events = make_transport(hanging_session, connect_timeout=0.05)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(hanging_session.urls) >= 2)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert ts.calls == 2
    assert 1.0 <= sleeps.delays[0] <= 1.2  # one backoff between the attempts


# --- handshake rejections ---


@pytest.mark.parametrize("status", [404, 410])
async def test_deterministic_rejection_raises_unsupported(sleeps, session, status):
    session.enqueue(handshake_error(status))
    transport, _ts, _events = make_transport(session)
    with pytest.raises(TransportUnsupportedError, match=str(status)):
        await transport.run()


async def test_mid_run_401_invalidates_and_reconnects(sleeps, session):
    session.enqueue(
        FakeWS(
            [
                text({"event": "update", "devid": "d", "data": {"path": "/p", "body": {}}}),
                close_entry(1006),
            ],
        ),
    )
    session.enqueue(handshake_error(401))
    transport, ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(session.urls) >= 3)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert ts.invalidated == [WsCredentials(token="t2", user_id="u2")]
    assert "token=t3&user_id=u3" in session.urls[2]


async def test_three_consecutive_rejection_rounds_raise_unsupported(sleeps, session, caplog):
    # the final TransportUnsupportedError detaches the realistic
    # (URL-bearing) ClientResponseError from its chain and the _cycle
    # failure path logs the exception TYPE only - no token, no URL
    caplog.set_level(
        logging.DEBUG, logger="custom_components.smartbox.smartbox_protocol.ws_user"
    )
    for _ in range(3):
        session.enqueue(handshake_error(401))
    transport, ts, _events = make_transport(session)
    with pytest.raises(TransportUnsupportedError) as excinfo:
        await transport.run()
    assert ts.calls == 3
    assert len(ts.invalidated) == 3
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
            chain=True,
        ),
    )
    for fragment in ("token=", "ws_user?token", "api.example.test"):
        assert fragment not in rendered
        assert fragment not in caplog.text


async def test_same_token_reissued_still_demotes(sleeps, session):
    # A no-op refresh can re-mint a byte-identical token (verified wire
    # behavior); demotion counts rejection ROUNDS, not token identity.
    session.enqueue(handshake_error(401))
    session.enqueue(handshake_error(401))
    session.enqueue(handshake_error(401))
    token_source = FakeTokenSource(fixed="REISSUED")
    transport, _ts, _events = make_transport(session, token_source=token_source)
    with pytest.raises(TransportUnsupportedError):
        await transport.run()
    assert token_source.calls == 3
    assert len(token_source.invalidated) == 3
    assert token_source.invalidated == [
        WsCredentials(token="REISSUED", user_id="u-fixed"),
    ] * 3
    # each rejection round paused with escalating backoff (n = round count)
    assert 1.6 <= sleeps.delays[0] <= 2.4
    assert 3.2 <= sleeps.delays[1] <= 4.8


async def test_rejection_rounds_pause_escalates_not_hammers(sleeps, session):
    # one rejection, then a parked connection: exactly one escalating
    # pause, no tight acquire/handshake loop around a rejection
    session.enqueue(handshake_error(401))
    transport, ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(session.urls) >= 2)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert sleeps.delays[0] == pytest.approx(2.0, abs=0.4)
    assert ts.calls == 2


async def test_served_cycle_resets_rejection_streak(sleeps, session):
    # 1 rejection, a served cycle, then only 2 consecutive rejections:
    # with the reset this must NOT raise TransportUnsupportedError
    session.enqueue(handshake_error(401))
    session.enqueue(FakeWS([close_entry(1000)]))
    session.enqueue(handshake_error(401))
    session.enqueue(handshake_error(401))
    transport, ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(session.urls) >= 5)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert transport.facts["auth_rejection_streak"] == 0  # the served 5th connection reset it
    assert len(ts.invalidated) == 3


# --- short-cycle escalation ---


async def test_short_cycle_pause_escalates_capped_at_max_sleep(sleeps, session):
    for _ in range(6):
        session.enqueue(FakeWS([close_entry(1006)]))
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(sleeps.delays) >= 5)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    pauses = sleeps.delays[:5]
    for n, delay in enumerate(pauses, start=1):
        base = 2.0**n
        assert min(1.0 * base, 30.0) <= delay <= min(1.2 * base, 30.0) + 1e-9
    assert pauses[4] <= 30.0  # capped at max_sleep


async def test_long_lived_cycle_resets_short_streak(sleeps, session):
    transport, _ts, _events = make_transport(session)
    transport._short_cycles = 1
    pause = transport._between_cycles_pause(
        wu._CycleOutcome(served=True, duration=60.0),
    )
    assert pause == pytest.approx(1.0)
    assert transport._short_cycles == 0


async def test_short_cycle_backoff_saturates_instead_of_overflowing(sleeps, session):
    """A pathological kill-loop sleeps the cap instead of killing the run.

    ``2**n`` raises OverflowError once a server sustains ~1024 short
    cycles, which would end run() and restart it in a loop; the shared
    backoff helper saturates the exponent, so the pause stays at the cap.
    """
    transport, _ts, _events = make_transport(session)
    assert transport._jittered_backoff(1024) == pytest.approx(30.0)
    transport._short_cycles = 1024
    pause = transport._between_cycles_pause(
        wu._CycleOutcome(served=True, duration=1.0),
    )
    assert pause == pytest.approx(30.0)
    assert transport._short_cycles == 1025


# --- token source failures ---


async def test_acquire_auth_error_propagates(sleeps, session):
    transport, _ts, _events = make_transport(
        session,
        token_source=FakeTokenSource(fail_with=AuthError("refresh failed")),
    )
    with pytest.raises(AuthError):
        await transport.run()


# --- lifecycle ---


async def test_close_bounded_unparks_run(sleeps, session):
    transport, _ts, _events = make_transport(session)  # parks in receive()
    run_task = asyncio.create_task(transport.run())
    await until(lambda: bool(session.urls))
    await asyncio.wait_for(transport.close(), 2.0)
    await asyncio.wait_for(run_task, 2.0)
    assert run_task.done()
    assert run_task.exception() is None
    assert transport._closed.is_set()
    await asyncio.wait_for(transport.close(), 2.0)  # idempotent


async def test_run_is_terminal_after_close(sleeps, session):
    transport, _ts, _events = make_transport(session)
    await transport.close()
    run_task = asyncio.create_task(transport.run())
    await asyncio.wait_for(run_task, 2)
    assert session.urls == []  # never connected


async def test_run_restart_resets_closed_event(sleeps, session):
    """A restarted run() clears the previous exit event.

    Without the reset, close() after the second start would see the
    first run's already-set event and not wait for the second run.
    """
    transport, ts, _events = make_transport(
        session, token_source=FakeTokenSource(fail_with=AuthError("refresh failed"))
    )
    run1 = asyncio.create_task(transport.run())
    with pytest.raises(AuthError):
        await asyncio.wait_for(run1, 2)
    assert transport._closed.is_set()
    ts.fail_with = None  # restart succeeds
    run2 = asyncio.create_task(transport.run())
    await until(lambda: bool(session.urls))
    assert not transport._closed.is_set()  # fresh event for THIS run
    await transport.close()
    await asyncio.wait_for(run2, 2)


async def test_run_twice_concurrently_raises(sleeps, session):
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: bool(session.urls))
    with pytest.raises(RuntimeError):
        await transport.run()
    await transport.close()
    await asyncio.wait_for(run_task, 2)


# --- secrets hygiene ---


async def test_no_token_in_logs(sleeps, session, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.smartbox.smartbox_protocol.ws_user")
    session.enqueue(
        FakeWS(
            [
                text({"event": "update", "devid": "d", "data": {"path": "/p", "body": {}}}),
                close_entry(1006),
            ],
        ),
    )
    session.enqueue(handshake_error(401))
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(session.urls) >= 3)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    log_text = caplog.text
    for fragment in ("t1", "u1", "t2", "u2", "t3", "u3", "ws_user?token", API_HOST):
        assert fragment not in log_text


async def test_connect_failure_logs_type_name_only(sleeps, session, caplog):
    """The ``_cycle`` swallow path logs the exception TYPE only.

    The URL (which carries the raw token) must never reach the logs.
    """
    caplog.set_level(
        logging.DEBUG, logger="custom_components.smartbox.smartbox_protocol.ws_user"
    )
    session.enqueue(handshake_error(503))  # realistic: its text bears the URL
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(session.urls) >= 2)  # failed attempt + retry
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    log_text = caplog.text
    assert "ClientResponseError" in log_text  # type name IS logged
    for fragment in ("ws_user?token", "token=", "api.example.test"):
        assert fragment not in log_text


# --- URL building / JWT helper ---


async def test_facts_track_running_and_close(sleeps, session):
    """Diagnostics facts: running flag flips, close code is recorded."""
    transport, _ts, _events = make_transport(session)
    assert transport.facts == {
        "running": False,
        "short_cycle_streak": 0,
        "auth_rejection_streak": 0,
        "last_close_code": None,
        "last_end_reason": None,
    }
    session.enqueue(FakeWS([text({"event": "update", "devid": "d", "data": {"path": "/p", "body": {}}}), close_entry(1006)]))
    # Park the run loop after the first cycle: with a 0.01 s poll the
    # facts window must stay readable (the next connect clears them).
    sleeps.gated = True
    run_task = asyncio.create_task(transport.run())
    await until(lambda: transport.facts["last_close_code"] == 1006)
    assert transport.facts["running"] is True
    assert transport.facts["last_end_reason"] == "close_frame"
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert transport.facts["running"] is False


async def test_facts_record_exception_death(sleeps, session):
    """Exception-deaths record too (1006/exception, no close frame)."""
    transport, _ts, events = make_transport(session)
    session.enqueue(FakeWS([aiohttp.ClientError("boom")]))  # dies in receive()
    # Park the run loop after the death: with a 0.01 s poll the facts
    # window must stay readable (the next connect clears them).
    sleeps.gated = True
    run_task = asyncio.create_task(transport.run())
    await until(lambda: transport.facts["last_end_reason"] == "exception")
    assert transport.facts["last_close_code"] == 1006  # abnormal default
    # The feed's only outage signal is SocketClosed; a death that never
    # reaches _serve's normal exit must still report it.
    assert any(isinstance(e, SocketClosed) for e in events)
    await transport.close()
    await asyncio.wait_for(run_task, 2)


class SendFailWS(FakeWS):
    """A socket whose first write fails (accept-then-kill shape)."""

    async def send_str(self, _data: str) -> None:
        msg = "send failed"
        raise aiohttp.ClientError(msg)


async def test_send_failure_reports_socket_closed(sleeps, session):
    """A failure before ``_serve`` still reports the socket ended."""
    transport, _ts, events = make_transport(session)
    session.enqueue(SendFailWS())
    sleeps.gated = True
    run_task = asyncio.create_task(transport.run())
    await until(lambda: any(isinstance(e, SocketClosed) for e in events))
    closed = next(e for e in events if isinstance(e, SocketClosed))
    assert closed.code == 1006
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_error_message_is_not_labelled_a_close_frame(sleeps, session):
    """``WSMsgType.ERROR`` is an exception death, not a peer close frame."""

    class ErrorWS(FakeWS):
        async def receive(self) -> aiohttp.WSMessage:
            return aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, None, None)

    transport, _ts, _events = make_transport(session)
    session.enqueue(ErrorWS())
    sleeps.gated = True
    run_task = asyncio.create_task(transport.run())
    await until(lambda: transport.facts["last_end_reason"] is not None)
    assert transport.facts["last_end_reason"] == "exception"
    assert transport.facts["last_close_code"] == 1006
    await transport.close()
    await asyncio.wait_for(run_task, 2)


async def test_close_abandons_a_handshake_that_completes_late(session, monkeypatch):
    """A socket completing after close() is closed, not served or sent to."""
    monkeypatch.setattr(wu, "_TEARDOWN_TIMEOUT", 0.01)
    hang = asyncio.Event()
    session._hang_first = hang
    session.enqueue(FakeWS())
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: session._hang_used)
    await transport.close()
    hang.set()
    await asyncio.wait_for(run_task, 2)
    assert session.last_ws is not None
    assert session.last_ws.sent == []


async def test_facts_reset_between_connects(sleeps, session):
    """A successful connect clears the previous socket's end facts."""
    transport, _ts, _events = make_transport(session)
    session.enqueue(FakeWS([close_entry(1006)]))
    # Park the run loop after the first cycle so the 1006 facts stay
    # readable, then release it for the second connect (facts reset).
    sleeps.gated = True
    run_task = asyncio.create_task(transport.run())
    await until(lambda: transport.facts["last_close_code"] == 1006)
    session.enqueue(FakeWS())  # second connect: facts must reset first
    sleeps.release()
    await until(lambda: len(session.urls) >= 2)
    await until(lambda: transport.facts["last_close_code"] is None)
    assert transport.facts["last_end_reason"] is None
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    assert transport.facts["running"] is False


def test_build_ws_url_basic():
    assert wu.build_ws_url(API_HOST, "abc.def.ghi", "u1") == (
        "wss://api.example.test:443/api/v2/ws_user?token=abc.def.ghi&user_id=u1"
    )


def test_build_ws_url_quoting_and_ports():
    assert wu.build_ws_url("https://api.example.test:8443", "a b", "u/2") == (
        "wss://api.example.test:8443/api/v2/ws_user?token=a%20b&user_id=u%2F2"
    )


def test_jwt_user_id_extracts_claim():
    assert wu.jwt_user_id(make_jwt({"userId": "u7", "exp": 1})) == "u7"


@pytest.mark.parametrize(
    "token", ["", "not-a-jwt", "notajwt", make_jwt({}), "aa.broken!.bb"]
)
def test_jwt_user_id_malformed_token_is_unusable_credentials(token):
    """A non-parseable JWT is unusable credentials (terminal, reauth-bound)."""
    with pytest.raises(AuthError) as excinfo:
        wu.jwt_user_id(token)
    if token:  # the empty token is trivially "contained" in any message
        assert token not in str(excinfo.value)  # hygiene: no token in messages


async def test_malformed_token_terminal_no_restart_ladder(sleeps):
    """A non-JWT token from the token source escapes run() as terminal.

    The supervise ladder stops on ``AuthError`` (engine doctrine); at the
    transport level: raised on the FIRST connect attempt, no network I/O,
    no backoff sleeps - no restart loop.
    """
    for _token in ("not-a-jwt", ""):
        ts = FakeTokenSource(fail_with=AuthError("malformed smartbox JWT"))
        transport, ts, _events = make_transport(FakeSession(), token_source=ts)
        with pytest.raises(AuthError):
            await transport.run()
        assert ts.calls == 1  # not re-acquired in a restart ladder
        assert sleeps.delays == []  # no backoff/restart ladder


async def test_granted_malformed_token_escapes_run_terminal(sleeps):
    """End-to-end: a grant payload with a non-JWT access_token.

    Real ``TokenManager`` + ``ManagedTokenSource`` chain: the provider
    grants ``access_token="not-a-jwt"``; acquire() classifies it
    unusable credentials and run() dies before any connect attempt.
    """
    config = AuthConfig(
        api_host=API_HOST,
        basic_auth_creds="creds",
        serial_id="1234",
        username="user@example.test",
        password="pw",
    )
    grant_session = FakeRestSession(default=FakeResponse(payload={
        "access_token": "not-a-jwt",
        "refresh_token": "rt",
        "expires_in": 14400,
    }))
    ts = ManagedTokenSource(TokenManager(config, session=grant_session))
    transport = wu.WsUserTransport(ts, API_HOST, session=FakeSession())
    with pytest.raises(AuthError):
        await transport.run()
    assert len(grant_session.calls) == 1  # one grant, no retry ladder
    assert sleeps.delays == []


# --- token-expiry cut labelling ---


def test_jwt_expiry_reads_exp_and_tolerates_junk():
    assert wu.jwt_expiry(make_jwt({"userId": "u1", "exp": 1893456000})) == 1893456000.0
    assert wu.jwt_expiry("not-a-jwt") is None
    assert wu.jwt_expiry(make_jwt({"userId": "u1"})) is None  # no exp claim


async def test_close_at_token_expiry_is_labelled_expected(sleeps, session):
    """The vendor's scheduled cut reaches core as ``expected=True``."""
    expired = make_jwt({"userId": "u1", "exp": int(time.time()) - 5})
    transport, _ts, events = make_transport(session, FakeTokenSource(fixed=expired))
    session.enqueue(FakeWS([close_entry(1006)]))
    sleeps.gated = True  # park the loop: the between-cycle pause holds
    run_task = asyncio.create_task(transport.run())
    try:
        await until(lambda: any(isinstance(e, SocketClosed) for e in events))
        closed = next(e for e in events if isinstance(e, SocketClosed))
        assert closed.code == 1006
        assert closed.expected is True
    finally:
        await transport.close()
        await asyncio.wait_for(run_task, 2)


async def test_unexplained_close_is_not_labelled_expected(sleeps, session):
    """A close with a token still far from expiry must not be excused."""
    fresh = make_jwt({"userId": "u1", "exp": int(time.time()) + 7200})
    transport, _ts, events = make_transport(session, FakeTokenSource(fixed=fresh))
    session.enqueue(FakeWS([close_entry(1006)]))
    sleeps.gated = True
    run_task = asyncio.create_task(transport.run())
    try:
        await until(lambda: any(isinstance(e, SocketClosed) for e in events))
        closed = next(e for e in events if isinstance(e, SocketClosed))
        assert closed.expected is False
    finally:
        await transport.close()
        await asyncio.wait_for(run_task, 2)


def test_closed_at_token_expiry_uses_a_skew_margin(sleeps, session):
    """A close a little before ``exp`` still counts as the scheduled cut."""
    transport, _ts, _events = make_transport(session)
    transport._token_exp_at_connect = time.time() + 20
    assert transport._closed_at_token_expiry() is True
    transport._token_exp_at_connect = time.time() + 3600
    assert transport._closed_at_token_expiry() is False
    transport._token_exp_at_connect = None  # no readable exp: not excused
    assert transport._closed_at_token_expiry() is False


# --- knob validation ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"heartbeat": 0},
        {"connect_timeout": 0},
        {"should_sync_debounce": -0.5},
    ],
)
def test_bad_knobs_rejected(kwargs):
    with pytest.raises(ValueError, match=next(iter(kwargs))):
        wu.WsUserTransport(FakeTokenSource(), API_HOST, session=FakeSession(), **kwargs)


def test_transport_knobs_reach_the_instance():
    transport = wu.WsUserTransport(
        FakeTokenSource(), API_HOST, heartbeat=30.0, session=FakeSession()
    )
    assert isinstance(transport, Transport)
    assert transport._heartbeat == 30.0


# --- seam relocation re-exports (TokenSource/WsCredentials moved to transport.py) ---


def test_seam_types_importable_from_both_paths():
    from custom_components.smartbox.smartbox_protocol.transport import (  # noqa: PLC0415 - locality of the guard
        TokenSource as TSFromTransport,
        WsCredentials as WsCredsFromTransport,
    )

    assert wu.TokenSource is TSFromTransport
    assert wu.WsCredentials is WsCredsFromTransport


async def test_connect_failure_warnings_throttled(sleeps, session, caplog):
    """An outage emits 2 WARNINGs per cycle (first + last attempt).

    Retried attempts log at DEBUG so an outage is not 10 WARNINGs per
    connect cycle.
    """
    caplog.set_level(
        logging.DEBUG, logger="custom_components.smartbox.smartbox_protocol.ws_user"
    )
    for _ in range(wu._RECONNECT_ATTEMPTS):
        session.enqueue(handshake_error(503))
    transport, _ts, _events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    await until(lambda: len(session.urls) >= wu._RECONNECT_ATTEMPTS + 1)
    await transport.close()
    await asyncio.wait_for(run_task, 2)
    attempts = [
        r
        for r in caplog.records
        if "connect attempt" in r.getMessage() and r.levelno >= logging.WARNING
    ]
    assert [r.levelno for r in attempts] == [logging.WARNING, logging.WARNING]
    assert attempts[0].getMessage() == (
        "ws_user connect attempt 1 failed: ClientResponseError"
    )
    assert attempts[1].getMessage() == (
        f"ws_user connect attempt {wu._RECONNECT_ATTEMPTS} failed:"
        " ClientResponseError"
    )
    debug_attempts = [
        r
        for r in caplog.records
        if "connect attempt" in r.getMessage() and "failed" in r.getMessage()
        and r.levelno == logging.DEBUG
    ]
    assert len(debug_attempts) == wu._RECONNECT_ATTEMPTS - 2
    # one DEBUG line at cycle exhaustion
    assert any("exhausted" in r.getMessage() for r in caplog.records)


async def test_a_silent_socket_is_torn_down_as_dead(monkeypatch, sleeps):
    """A socket that delivers nothing past the bound is closed and retried.

    The heartbeat covers a dead path, not a server that answers PINGs and
    sends no frames: the silence bound routes that case through the ordinary
    close -> feed-window path.
    """
    monkeypatch.setattr(wu, "_SILENCE_TIMEOUT", 0.05)
    session = FakeSession()
    # the snapshot lands, then the socket delivers nothing at all
    session.enqueue(FakeWS([text({"event": "all_data", "data": []})]))
    session.enqueue(FakeWS([("close", 1006)]))  # the reconnect, then a close
    transport, _ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    try:
        await until(lambda: any(isinstance(e, SocketClosed) for e in events))
        closed = [e for e in events if isinstance(e, SocketClosed)]
        assert closed[0].code == 1006
        assert closed[0].expected is False
        # the teardown feeds the ordinary reconnect path
        await until(lambda: len(session.urls) >= 2)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        await transport.close()


async def test_a_socket_without_a_snapshot_is_torn_down(monkeypatch, sleeps):
    """No all_data within the connect window closes the socket (api-ws-user §1.1).

    A server that accepts the socket and never answers the all_data request
    is a wedged session, not a quiet one: the watchdog routes it through the
    ordinary close -> reconnect path.
    """
    monkeypatch.setattr(wu, "_SNAPSHOT_TIMEOUT", 0.05)
    session = FakeSession()
    session.enqueue(FakeWS())  # connected; never delivers the snapshot
    session.enqueue(FakeWS([text({"event": "all_data", "data": []})]))
    transport, _ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    try:
        await until(lambda: any(isinstance(e, Snapshot) for e in events))
        assert any(isinstance(e, SocketClosed) for e in events)
        await until(lambda: len(session.urls) >= 2)  # the reconnect delivered it
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        await transport.close()


async def test_a_chatty_socket_without_a_snapshot_is_still_torn_down(
    monkeypatch, sleeps
):
    """Updates must not reset the snapshot watchdog (it is an absolute deadline)."""
    monkeypatch.setattr(wu, "_SNAPSHOT_TIMEOUT", 0.05)
    session = FakeSession()
    chatty = ChattyWS(interval=0.01)
    session.enqueue(chatty)
    session.enqueue(FakeWS([text({"event": "all_data", "data": []})]))
    transport, _ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    try:
        await until(lambda: any(isinstance(e, Snapshot) for e in events))
        assert chatty.updates > 1, "the socket was chatty, not silent"
        assert any(isinstance(e, SocketClosed) for e in events)
        await until(lambda: len(session.urls) >= 2)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        await transport.close()


async def test_a_scalar_update_body_reaches_the_core(sleeps, session):
    """The box power-limit frame may push the bare digit string (api-ws-user §1.4)."""
    session.enqueue(
        FakeWS(
            [
                text({"event": "all_data", "data": []}),
                text(
                    {
                        "event": "update",
                        "devid": "dev1",
                        "data": {
                            "path": "/htr_system/power_limit",
                            "body": "2500",
                        },
                    }
                ),
            ]
        )
    )
    transport, _ts, events = make_transport(session)
    run_task = asyncio.create_task(transport.run())
    try:
        await until(lambda: any(isinstance(e, Update) for e in events))
        assert [e for e in events if isinstance(e, Update)] == [
            Update(dev_id="dev1", path="/htr_system/power_limit", body="2500")
        ]
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        await transport.close()

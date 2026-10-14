"""Chaos soak: the REAL stack under seeded, hostile server behavior.

The loopback suite pins individual wire behaviors; this drives the whole
stack (``TokenManager`` -> ``SmartboxClient`` -> ``WsUserTransport`` ->
``SmartboxEngine``) against a local aiohttp server that dies, stalls,
5xxs and pushes lost frames on a seeded schedule, for a bounded wall
clock, asserting the engine's structural invariants after every step.

What it is for: the confirmation, availability and feed machinery only
misbehaves in INTERACTIONS - a window that never resolves, a timer that
outlives its deadlines, a close that does not report an outage. The
invariants here are the ones that machinery must never break:

- INV1 no deadline is past its expiry by more than a scheduling slack (a
  stuck entry would never resolve);
- INV2 no deadlines means no live timer task (a leaked timer would flip
  a node after its window resolved);
- INV3 availability is a tri-state and the feed a bool;
- INV4 teardown clears every deadline, the timer, and delivers nothing
  afterwards (a late notify drives removed entities);
- INV5 nothing outlives the test (the plugin's ``verify_cleanup`` fails
  the run on a lingering task, which is how a leaked watcher shows up).

The chaos is seeded and the phase-1 step count is fixed, so a failing run
names the seed and the actions that led to it. Windows are shrunk to
fractions of a second so a few seconds of wall clock covers many timeout
and reconnect cycles.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import test_utils, web
import pytest

from custom_components.smartbox.smartbox_core.engine import SmartboxEngine
from custom_components.smartbox.smartbox_protocol import ws_user as wu
from custom_components.smartbox.smartbox_protocol.auth import (
    AuthConfig,
    ManagedTokenSource,
)
from custom_components.smartbox.smartbox_protocol.client import NodeRef, SmartboxClient
from custom_components.smartbox.smartbox_protocol.errors import SmartboxError
from custom_components.smartbox.smartbox_protocol.ws_user import WsUserTransport
from tests.fakes import make_jwt
from tests.test_utils import armed_deadlines, until

if TYPE_CHECKING:
    from custom_components.smartbox.smartbox_core.state import Change

BASIC_CREDS = "cmVzZWxsZXJfc2VjcmV0"
SERIAL_ID = "1234"
EMAIL = "user@example.test"
PASSWORD = "hunter2"

GRANT = {
    "access_token": make_jwt({"userId": "u-soak", "exp": 9999999999}),
    "refresh_token": "rt",
    "expires_in": 14400,
}

DEV_ID = "dev1"
NODE = NodeRef(node_type="htr", addr="2")
NODE_KEY = "htr/2"

# Seeded: a failure is reproducible from this number.
SEED = 20261018
SOAK_SECONDS = 3.0
# The random phase's fixed step count (see the loop's comment).
CHAOS_STEPS = 400
# Windows are shrunk so a few seconds cover many timeout cycles.
UNAVAILABLE_DELAY = 0.2
WRITE_CONFIRM_TIMEOUT = 0.2
# A deadline may sit expired this long before the timer gets scheduled
# (loop starvation, not a stuck entry).
DEADLINE_SLACK = 1.5
# Budget for "this window resolves on its own" polls. The shrunk windows are
# 0.2 s; the budget is generous because a loaded CI box schedules slowly, and
# the poll returns as soon as the outcome holds.
RESOLVE_BUDGET = 10.0
# Kill the socket on this cadence as well as randomly, so a short soak
# always exercises the reconnect path.
KILL_EVERY = 13

STATUS = {
    "mode": "auto",
    "units": "C",
    "stemp": "20.0",
    "mtemp": "19.5",
    "locked": False,
    "sync_status": "ok",
}
SETUP = {"priority": "low", "window_mode_enabled": False}


class ChaosCloud:
    """Local aiohttp cloud the soak's chaos actions drive.

    Serves the REST surface the engine writes through and one ws_user
    connection; ``kill_socket`` aborts its TCP connection (the 1006 the
    vendor's scheduled cuts look like), ``push_status`` sends an update
    frame, and ``fail_next_post`` makes the next write a 503.
    """

    def __init__(self, rng: random.Random) -> None:
        self.host = ""
        self.rng = rng
        self.status = dict(STATUS)
        self.setup = dict(SETUP)
        self.fail_next_post = False
        self.post_delay = 0.0
        self.in_flight_posts = 0
        self.ws: web.WebSocketResponse | None = None
        self.ws_transport: asyncio.Transport | None = None
        self.connections = 0
        self.posts: list[dict[str, Any]] = []
        self.pushes = 0
        self.release = asyncio.Event()

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/client/token", self._token)
        app.router.add_get(
            "/api/v2/devs/{dev_id}/{node_type}/{addr}/status", self._get_status
        )
        app.router.add_get(
            "/api/v2/devs/{dev_id}/{node_type}/{addr}/setup", self._get_setup
        )
        app.router.add_post(
            "/api/v2/devs/{dev_id}/{node_type}/{addr}/status", self._post_status
        )
        app.router.add_post(
            "/api/v2/devs/{dev_id}/{node_type}/{addr}/setup", self._post_setup
        )
        app.router.add_get("/api/v2/ws_user", self._ws_user)
        return app

    async def _token(self, request: web.Request) -> web.Response:
        await request.post()
        return web.json_response(GRANT)

    async def _get_status(self, request: web.Request) -> web.Response:  # noqa: ARG002
        return web.json_response(dict(self.status))

    async def _get_setup(self, request: web.Request) -> web.Response:  # noqa: ARG002
        return web.json_response(dict(self.setup))

    async def _post_status(self, request: web.Request) -> web.Response:
        self.posts.append(await request.json())
        self.in_flight_posts += 1
        if self.post_delay:
            await asyncio.sleep(self.post_delay)
        if self.fail_next_post:
            self.fail_next_post = False
            return web.Response(status=503)
        # A real box pushes the confirming frame; the soak decides when
        # (that is the point: sometimes it never comes).
        return web.json_response({})

    async def _post_setup(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.posts.append(body)
        self.setup = dict(body)
        return web.json_response({})

    async def _ws_user(self, request: web.Request) -> web.StreamResponse:
        self.connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self.ws_transport = request.transport
        # The transport being aborted under us IS the chaos.
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await ws.receive()  # the client's all_data frame on open
            await ws.send_json({"event": "all_data", "data": self._snapshot()})
            async for _msg in ws:
                pass
        return ws

    def _snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "h1",
                "devs": [
                    {
                        "dev_id": DEV_ID,
                        "devData": {
                            "connected": True,
                            "nodes": [
                                {
                                    "type": "htr",
                                    "addr": "2",
                                    "name": "Heater",
                                    "status": dict(self.status),
                                    "setup": dict(self.setup),
                                },
                            ],
                        },
                    },
                ],
            },
        ]

    async def push_status(self, body: dict[str, Any]) -> None:
        """Push one update frame (a no-op while no socket is up)."""
        ws = self.ws
        if ws is None or ws.closed:
            return
        self.pushes += 1
        # The socket dying under the send is the same chaos as a kill.
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await ws.send_json(
                {
                    "event": "update",
                    "devid": DEV_ID,
                    "data": {"path": f"/{NODE_KEY}/status", "body": dict(body)},
                },
            )

    def kill_socket(self) -> None:
        """Abort the ws TCP connection (no close frame: abnormal 1006)."""
        transport, self.ws_transport = self.ws_transport, None
        self.ws = None
        if transport is not None:
            transport.abort()


@pytest.fixture
async def cloud(socket_enabled):
    """Run the chaos server for one test."""
    # A seeded PRNG, not a security primitive: reproducibility is the point.
    rng = random.Random(SEED)  # noqa: S311
    state = ChaosCloud(rng)
    server = test_utils.TestServer(state.make_app())
    await server.start_server()
    state.host = str(server.make_url("")).rstrip("/")
    try:
        yield state
    finally:
        state.release.set()
        await server.close()


@pytest.fixture
def fast_ws_backoff(monkeypatch):
    """Shrink the transport's reconnect backoff so reconnects fit the soak."""
    monkeypatch.setattr(wu, "_BACKOFF_FACTOR", 0.05)
    monkeypatch.setattr(wu, "_MAX_SLEEP", 0.2)


class SoakStack:
    """The real stack, wired the way the adapter wires it."""

    def __init__(self, cloud: ChaosCloud, session: aiohttp.ClientSession) -> None:
        config = AuthConfig(
            api_host=cloud.host,
            basic_auth_creds=BASIC_CREDS,
            serial_id=SERIAL_ID,
            username=EMAIL,
            password=PASSWORD,
        )
        self.client = SmartboxClient(config, session=session)
        self.engine = SmartboxEngine(
            self.client,
            unavailable_delay=UNAVAILABLE_DELAY,
            write_confirm_timeout=WRITE_CONFIRM_TIMEOUT,
        )
        self.transport = WsUserTransport(
            ManagedTokenSource(self.client.auth),
            config.api_host,
            session=session,
        )
        self.changes: list[Change] = []
        self.engine.subscribe(self._record)

    async def _record(self, change: Change) -> None:
        self.changes.append(change)

    def start(self) -> None:
        self.engine.supervise(self.transport)


def assert_invariants(engine: SmartboxEngine, applied: list[str]) -> None:
    """Assert the invariants the confirmation/availability machinery owns.

    ``applied`` is the action log so far: a failure names the seed and the
    actions that led to it, which is what makes the seeded run replayable.
    """
    context = f"seed={SEED} last actions={applied[-6:]}"
    for deadline in engine.deadlines:
        assert deadline.expires_in >= -DEADLINE_SLACK, (
            f"stuck {deadline.kind} deadline for {deadline.dev_id}/"
            f"{deadline.node_key}: expires_in={deadline.expires_in} ({context})"
        )
    if not engine.deadlines:
        assert engine.timer_armed is False, f"timer outlived its deadlines ({context})"
    for device in engine.world.devices.values():
        for node in device.node_states.values():
            assert node.available in (True, False, None)
            assert node.revision >= 0
    assert isinstance(engine.feed.available, bool)


async def apply_chaos(
    cloud: ChaosCloud, stack: SoakStack, step: int, applied: list[str]
) -> None:
    """Apply one seeded chaos action, recording it for failure messages."""
    engine = stack.engine
    rng = cloud.rng
    if step % KILL_EVERY == 0:
        applied.append(f"{step}:kill(periodic)")
        cloud.kill_socket()
    else:
        action = rng.choices(
            ("write", "push_ok", "push_lost", "kill", "fail_post", "idle"),
            weights=(4, 4, 3, 1, 1, 5),
        )[0]
        applied.append(f"{step}:{action}")
        if action == "write":
            op = rng.choice(
                (
                    lambda: engine.set_mode(
                        DEV_ID, NODE, rng.choice(("manual", "auto", "off"))
                    ),
                    lambda: engine.set_lock(DEV_ID, NODE, rng.choice((True, False))),
                    lambda: engine.set_window_mode(
                        DEV_ID, NODE, rng.choice((True, False))
                    ),
                ),
            )
            # A 5xx write is an expected outcome, not a soak failure.
            with contextlib.suppress(SmartboxError):
                await op()
        elif action == "push_ok":
            await cloud.push_status(
                {**STATUS, "mtemp": f"{rng.uniform(15.0, 25.0):.1f}"}
            )
        elif action == "push_lost":
            await cloud.push_status({"sync_status": "lost"})
        elif action == "kill":
            cloud.kill_socket()
        elif action == "fail_post":
            cloud.fail_next_post = True
    await asyncio.sleep(0.01)  # let the timers and the pump run


async def await_quiet_socket(cloud: ChaosCloud, engine: SmartboxEngine) -> None:
    """Wait until the ws is up, the feed has settled and frames arrive.

    The server's view of the socket is not enough: after a chaos kill the
    client may already be reconnecting while the aborted ws still looks open
    on this side, so a push into it would be lost. The wait therefore proves
    a round-trip on the CURRENT connection (a marker value the engine must
    store) before it returns.
    """
    for _ in range(10):
        await until(lambda: cloud.ws is not None and not cloud.ws.closed)
        await until(lambda: engine.feed.available)
        marker = f"{time.monotonic():.3f}"

        def _marker_seen(expect: str = marker) -> bool:
            node = engine.node(DEV_ID, NODE_KEY)
            return node is not None and node.status.get("mtemp") == expect

        await cloud.push_status({**STATUS, "mtemp": marker})
        try:
            await until(_marker_seen, budget=2.0)
        except TimeoutError:
            continue  # that connection was dead; wait for the next one
        return
    msg = "no frame ever round-tripped"
    raise AssertionError(msg)


async def flip_lock(engine: SmartboxEngine) -> None:
    """Write the child lock to the value the store does not hold yet.

    A value-equal write disarms its own confirmation by design, so a phase
    that wants a window to observe has to change something.
    """
    node = engine.node(DEV_ID, NODE_KEY)
    current = node.status.get("locked", False) if node is not None else False
    await engine.set_lock(DEV_ID, NODE, not current)


async def arm_lock_window(engine: SmartboxEngine, cloud: ChaosCloud) -> None:
    """Arm one lock-write confirmation window, retrying a transient failure.

    The soak asserts invariants, not that any single write succeeded: a 5xx
    is a legitimate server answer (the chaos injects them, and the flag can
    outlive the random phase), so it is retried here instead of failing.
    """
    cloud.fail_next_post = False
    for _ in range(5):
        with contextlib.suppress(SmartboxError):
            await flip_lock(engine)
        if armed_deadlines(engine, "write"):
            return
        await asyncio.sleep(0.05)
    msg = "no confirmation window could be armed"
    raise AssertionError(msg)


async def assert_deadlines_are_clear(
    cloud: ChaosCloud, engine: SmartboxEngine
) -> None:
    """Wait out every outstanding window so the phases start from quiescence."""
    cloud.fail_next_post = False  # a leftover 5xx would poison the phases
    try:
        await until(lambda: not armed_deadlines(engine), budget=RESOLVE_BUDGET)
    except TimeoutError as err:
        msg = f"a window never resolved: {armed_deadlines(engine)}"
        raise AssertionError(msg) from err


async def assert_lost_window_resolves(
    cloud: ChaosCloud, engine: SmartboxEngine
) -> None:
    """Assert a lost frame with no follow-up resolves into Unavailable.

    Nothing else touches the node here, so a window that only ever resolves
    because some later ok frame cancels it fails this.
    """
    await cloud.push_status({"sync_status": "lost"})
    await until(lambda: len(armed_deadlines(engine, "lost")) == 1)
    try:
        await until(
            lambda: not armed_deadlines(engine, "lost"), budget=RESOLVE_BUDGET
        )
    except TimeoutError as err:
        msg = "the lost window never resolved"
        raise AssertionError(msg) from err
    assert engine.node(DEV_ID, NODE_KEY).available is False


async def assert_pending_write_resolves(
    cloud: ChaosCloud, engine: SmartboxEngine
) -> None:
    """Assert a write the box never confirms resolves into Unavailable.

    The server pushes no confirming frame of its own, so this window can
    only end in the timeout.
    """
    await arm_lock_window(engine, cloud)
    try:
        await until(
            lambda: not armed_deadlines(engine, "write"), budget=RESOLVE_BUDGET
        )
    except TimeoutError as err:
        msg = "the confirmation window never resolved"
        raise AssertionError(msg) from err
    assert engine.node(DEV_ID, NODE_KEY).available is False


async def assert_overlapping_deadlines_resolve(
    cloud: ChaosCloud, engine: SmartboxEngine
) -> None:
    """Assert a later deadline survives an earlier one expiring.

    One timer serves every node deadline, so it must re-arm for whatever is
    still outstanding after it fires. A window that only ever resolves
    because some later action armed a fresh timer would slip through.
    """
    await cloud.push_status({"sync_status": "lost"})
    await until(lambda: len(armed_deadlines(engine, "lost")) == 1)
    await asyncio.sleep(UNAVAILABLE_DELAY / 2)
    await arm_lock_window(engine, cloud)  # a LATER deadline
    try:
        await until(lambda: not armed_deadlines(engine), budget=RESOLVE_BUDGET)
    except TimeoutError as err:
        msg = f"a deadline never resolved: {armed_deadlines(engine)}"
        raise AssertionError(msg) from err


@pytest.mark.asyncio
async def test_soak_chaos_keeps_the_engine_consistent(cloud, fast_ws_backoff):
    """Drive the real stack through seeded chaos, asserting every step."""
    async with aiohttp.ClientSession() as session:
        stack = SoakStack(cloud, session)
        engine = stack.engine
        stack.start()
        try:
            await until(lambda: engine.node(DEV_ID, NODE_KEY) is not None)

            # Phase 1: seeded chaos - the interleavings. The step count is
            # fixed so the PRNG stream is reproducible; the wall clock only
            # caps how long a pathological step may hold the loop.
            deadline = time.monotonic() + SOAK_SECONDS
            applied: list[str] = []
            for step in range(CHAOS_STEPS):
                if time.monotonic() >= deadline:
                    break
                await apply_chaos(cloud, stack, step, applied)
                assert_invariants(engine, applied)
            step = len(applied) - 1

            # The soak must actually have exercised the hostile paths, or it
            # is a slow no-op that would hide a regression.
            assert len(applied) > 10, (
                f"only {len(applied)} chaos steps ran (seed={SEED})"
            )
            assert cloud.posts, "no write ever reached the server"
            assert cloud.connections >= 2, (
                f"the socket never died and reconnected ({cloud.connections})"
            )
            assert cloud.pushes >= 1, "no frame was ever pushed"

            # Phase 2: quiescence - once the traffic stops, every deadline
            # must RESOLVE on its own. The random phase alone cannot pin this:
            # a window that never fires still disappears as soon as some later
            # frame cancels it.
            await await_quiet_socket(cloud, engine)
            await assert_deadlines_are_clear(cloud, engine)
            await assert_lost_window_resolves(cloud, engine)
            await assert_pending_write_resolves(cloud, engine)
            await assert_overlapping_deadlines_resolve(cloud, engine)
            assert_invariants(engine, applied)

            # INV6: only the documented terminal errors may be on the task.
            task = engine.supervision_task
            if task is not None and task.done() and not task.cancelled():
                exc = task.exception()
                assert exc is None, f"supervision died with {exc!r}"

            # Phase 3 setup: leave a deadline outstanding across the teardown,
            # the way an in-flight write during an unload does.
            await arm_lock_window(engine, cloud)
            assert engine.timer_armed is True, "no deadline was armed"
        finally:
            await engine.stop()
            with contextlib.suppress(Exception):
                await stack.transport.close()

        # INV4: teardown clears every deadline and the timer, and nothing
        # is delivered afterwards (a late notify drives removed entities).
        assert engine.deadlines == ()
        assert engine.timer_armed is False
        delivered = len(stack.changes)
        await asyncio.sleep(0.05)
        assert len(stack.changes) == delivered, "notified after stop()"


@pytest.mark.asyncio
async def test_soak_write_in_flight_across_teardown_is_silent(
    cloud, fast_ws_backoff
):
    """A write whose POST outlives stop() must deliver nothing.

    HA unloads platforms before teardown and does not await in-flight
    service calls, so this is a real sequence: the POST lands after
    ``stop()`` and its optimistic merge must not drive entities that are
    already being removed.
    """
    async with aiohttp.ClientSession() as session:
        stack = SoakStack(cloud, session)
        engine = stack.engine
        stack.start()
        try:
            await until(lambda: engine.node(DEV_ID, NODE_KEY) is not None)
            # Longer than the confirmation window, so the POST is still in
            # flight when the engine stops.
            cloud.post_delay = 0.4
            in_flight = asyncio.create_task(flip_lock(engine))
            await until(lambda: cloud.in_flight_posts >= 1)
            await engine.stop()
            delivered = len(stack.changes)
            await in_flight  # the POST completes after the stop
            await asyncio.sleep(0.05)
            assert len(stack.changes) == delivered, "notified after stop()"
            assert armed_deadlines(engine, "write") == []
        finally:
            await engine.stop()
            with contextlib.suppress(Exception):
                await stack.transport.close()

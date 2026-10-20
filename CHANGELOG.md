# Changelog

See GitHub release notes: https://github.com/ajtudela/hass-smartbox/releases

## 3.0.0 (unreleased)

This release runs on a purpose-built three-layer
stack (`smartbox_protocol` auth/client/transport/writes →
`smartbox_core` versioned state store + engine → the HA adapter) and
the legacy `smartbox` library runtime path is gone.

### Breaking

- **socket_io fallback removed.** Only the `ws_user` websocket
  transport (the vendor web apps' channel) is supported: setup probes
  the API host once and an account whose host does not serve `ws_user`
  cannot work (config entry error "unsupported"). Stay on 2.6.x for
  non-`ws_user` resellers.
- The underlying `smartbox` library is NO LONGER a runtime dependency:
  the reseller table, the node-type enum and the shared backoff helper
  are absorbed into `custom_components/smartbox/`
  (`smartbox_protocol/reseller.py`, `smartbox_protocol/retry.py`,
  `const.py`), and `manifest.json` ships empty `requirements`.
  Users of the library's CLI tooling install it separately.
- The `smartbox_protocol`/`smartbox_core` layers ship inside
  `custom_components/smartbox/` (HACS and manual installs carry
  everything; no extra package install step).
- The `SMARTBOX_WS_BACKEND` constant was removed together with the
  socket_io fallback. It was a source constant in `const.py`, never a
  config-entry option, so there is no setting to delete: an installation
  that had edited it to keep the legacy per-device transport has to stay
  on 2.6.x instead.

### Added / changed

- Same entity surface as 2.6.0 (climate, sensors, binary sensor,
  switches, numbers, selects, services; registry identities are
  unchanged, so entities keep their history).
- Write confirmation: every node write arms a confirmation window —
  entities apply the write optimistically and turn Unavailable when
  the device never confirms (matches the vendor app's behavior).
- Node availability tracking moved to the engine (lost-frame windows
  + write confirmation), replacing the library's availability surface.
- After a restart, entities may briefly render Unavailable until the
  first websocket snapshot arrives.
- Diagnostics now report the supervised `ws_user` transport,
  supervision state, token expiry, the feed (websocket) state, the
  engine store and the deadlines that will flip a node Unavailable.
- System health reads the API's version/health endpoints over the new
  client.
- A feed outage is visible: when the websocket has been silent for more
  than 10 seconds, entities render Unavailable instead of showing stale
  values, and the outage is logged once. The vendor's scheduled
  token-expiry reconnect (every ~4 hours, a few seconds) is recognised
  and deliberately excluded from that signal.
- A repair issue reports an endpoint that keeps refusing the `ws_user`
  handshake (`ws_user_flapping`). The integration re-probes it in place
  behind a capped backoff, so a backend that starts serving again is
  picked up without a reload.
- `tools/live_probe.py` is the CLI used to verify wire behaviour
  against real hardware (reads and one guarded write at a time;
  `--i-know-this-writes-to-my-heater` is required for writes).
- The wire-fact record lives in this repo (`docs/api-contract.md`, plus
  the app statics it cites in `docs/webapi-spec.md`); code comments cite
  those files, so a clone or archive of this repo is self-contained.
- The schedule sensor follows the websocket now (prog arrives in the
  snapshot and in pushed frames); its old 15-minute REST refetch is
  gone.
- Turning the box Away switch refreshes the climate cards immediately
  (2.6.0 kept the previous preset until an unrelated frame arrived).
- The "no power limit" switch reports that it has nothing to restore
  (2.6.0 logged a warning and did nothing, so the toggle snapped back
  silently).
- Reseller table: Tevolve (`api-tevolve`, serial 15, web app
  `https://tevolve.termoweb.net/`) is selectable. Its tenant constants
  were decoded from the vendor's web bundle; a live login is still
  unverified.

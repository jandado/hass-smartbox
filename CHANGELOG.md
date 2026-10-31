# Changelog

See GitHub release notes: https://github.com/ajtudela/hass-smartbox/releases

## Unreleased

- Config-entry diagnostics now report first-setup statistics-import progress:
  pending and completed nodes, consecutive failures, the last error and the
  remaining backoff.
- A heater or box added to the account after setup is added in place: the
  snapshot event refreshes the REST identity and the platform builders create
  the new device and node entities. A failed refresh falls back to a repair
  issue asking for a reload. Existing entities are left in place when
  membership shrinks, so a temporary backend glitch cannot remove their
  history or unique ids. Diagnostics reports the pending additions.
- User-visible service and entity errors are translated (de/en/es/fr) and
  raised as `ServiceValidationError` for invalid input or `HomeAssistantError`
  for a state restriction or failed write.
- The existing account's username or password can be corrected without
  deleting the integration entry, so existing entities and their history are
  kept. Changing the reseller, or switching accounts on an entry that already
  has entities, requires removing and re-adding the integration.
- System health reports every configured account, counts accounts without a
  live runtime, and labels them by reseller and index instead of the account
  username. The integration title is now “Smartbox”.
- Added a Home Assistant quality-scale audit. The manifest remains
  `"quality_scale": "custom"` until the recorded gaps are closed.

## 3.0.0-rc.1 (2026-10-20)

Release candidate for the 3.0.0 line; the soak and the remaining
hardware-gated items still precede the final release.

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

- The 2.6.0 entity surface (climate, sensors, binary sensor, switches,
  numbers, selects, services) plus the node-level power-limit number and
  "no node power limit" switch that PMO nodes expose; existing registry
  identities are unchanged, so entities keep their history.
- The README lists every supported reseller with its provider link; a
  test keeps that table in step with the reseller table. The 2026-10-20
  public-app check records that EHC and Electrorad still serve the
  legacy socket.io app, while the other eleven use `ws_user`.
- Write confirmation: every node write arms a confirmation window —
  entities apply the write optimistically and turn Unavailable when
  the device never confirms (matches the vendor app's behavior). The
  6 s countdown starts when the POST returns, so a token refresh or a
  slow request cannot expire it mid-flight.
- A websocket that dies by exception after the handshake reports the
  close to the feed (and the supervisor opens the same grace window on
  an abnormal run exit), so stale values cannot keep rendering as
  available.
- Node availability derives from both the status and setup lost
  markers; a stale ok status no longer hides a lost setup slot.
- The statistics import is skipped cleanly on installations without
  the recorder (instead of raising out of the interval job and
  re-reading three years every tick).
- Live 2026-10-20: the `max_stemp_limit "0.0"` setup write is accepted
  and persists, the app-shaped 4-key `/status` prog-temp body is
  accepted and echoed, and the sentinel's enforcement is confirmed
  (24.5 clamped to 24.0 with the limit at 24.0; accepted with 0.0).
  Both probes restored the original values. The dedicated `/prog_temps`
  endpoint remains unverified.
- Programme temperatures are clamped the way the vendor app does before
  the write: per-profile bounds, the ice/eco/comfort offset (0.5 °C /
  1 °F), and a corrected 4-key status body after lowering the max limit.
  The changed profile is exempt from the neighbour adjustment.
- Node availability tracking moved to the engine (lost-frame windows
  + write confirmation), replacing the library's availability surface.
- After a restart, entities may briefly render Unavailable until the
  first websocket snapshot arrives.
- Diagnostics now report the supervised `ws_user` transport,
  supervision state, token expiry, the feed (websocket) state, the
  engine store and the deadlines that will flip a node Unavailable.
- System health reads the API's version/health endpoints over the new
  client.
- A feed outage is visible: when the socket closes and no frame arrives
  within 10 seconds, entities render Unavailable instead of showing stale
  values, and the outage is logged once. An open socket may be quiet for
  minutes without being treated as down. The vendor's scheduled
  token-expiry reconnect (every ~4 hours, a few seconds) normally
  delivers a frame inside that window and clears it silently.
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
- Consumption statistics build their hourly rows in UTC, so the import
  also works in Home Assistant timezones that are not a whole hour from
  UTC (for example +05:30 or +05:45).
- Turning the box Away switch refreshes the climate cards immediately
  (2.6.0 kept the previous preset until an unrelated frame arrived).
- The "no power limit" switch reports that it has nothing to restore
  (2.6.0 logged a warning and did nothing, so the toggle snapped back
  silently).
- Reseller table: Tevolve (`api-tevolve`, serial 15, web app
  `https://tevolve.termoweb.net/`) is selectable. Its tenant constants
  were decoded from the vendor's web bundle; a live login is still
  unverified.
- Boost follows the vendor app's `boost_config` dispatch: 1/2 use the
  status/setup path, 3 (full) posts `/boost_mode`
  (`{active, mode, temp?, time?}`), including the runback and
  advanced-slot modes. The full path is app-evidenced; live acceptance
  is still pending.
- Full-boost nodes expose Runback and Advanced boost as climate presets
  (when `boost_modes_available` advertises them). The boost/runback
  number entities remain defaults; the climate target edits the active
  temperature, and `boost_mode`, `boost_end_time` and
  `boost_remaining_minutes` are presented as climate attributes. A new
  `smartbox.set_boost_mode` service covers active duration edits and
  explicit mode selection.
- Runback uses the node's own limits while it is active (frost/setback
  lower bound, `max_runback_temp` upper bound clamped by the global
  limit). `extra_options.version >= 2` stores boost/runback temperatures
  in Celsius; older nodes keep them in the display unit, as the app does.

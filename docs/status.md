# Project status

> **Role.** The short, curated record of what is in flight: the state of the
> 3.0.0 line, what the soak is watching, and the open items that wait on
> hardware or on a decision. Long-lived facts live in the code and the other
> `docs/` files; completed work lives in git.

*Updated 2026-10-20. Keep this a 90-second read and prune entries as they
close.*

## State of the 3.0.0 line

- The rewrite is complete and pushed to the fork's `rewrite` branch (over
  the released 2.6.0); `main` carries the released 2.6.x line until the
  maintainer releases 3.0.0.
- The 3.0.0 soak (extended live monitoring) runs on the maintainer's Home
  Assistant box, deployed from a `git archive` of the branch; the release
  decision waits on the soak.
- CI on the branch is green: ruff, mypy, hassfest, HACS, pytest on the
  newest allowed Home Assistant, `tox -e min` on the declared 2026.5.0
  floor, and both Codecov uploads.
- The Tevolve tenant (`api-tevolve`, serial 15) is selectable. Its
  constants came from the vendor web bundle; a live login is still
  unverified.
- Public web-app transport check (2026-10-20): 11 of the 13 provider
  apps ship the `ws_user` client; the EHC and Electrorad apps still
  serve the legacy socket.io app. The per-account setup probe remains
  authoritative.

## What the soak is watching

- The 2.6.0 checklist still applies: one socket for all devices, the
  availability windows, a write mid-soak, the 4 h token reconnect.
- Write confirmation: the 6 s countdown starts when the POST returns; the
  in-flight hold covers a token refresh or a slow request. Revisit if writes
  to a box still look slow.
- Token reconnect: revisit only if a cut lasts far longer than ~4 s or
  happens more often than every 4 h.

## Open items (hardware-gated)

`docs/api-contract.md` §7 is canonical for the wire questions and their
statuses; the max-limit `"0.0"` and 4-key prog-temp probes live in
`docs/api-contract.md` §5.2 and E28. The ones that need the maintainer's
hardware — or a season:

- `window_open` retry, cold night only: open the window AND door BEFORE
  starting heat, setpoint 23 °C, outside below 5 °C; success is
  `window_open: true` in the frame plus the heater self-pausing (active
  false, duty 0) while the temperature keeps falling.
- RTC `n` month indexing — waits for December (the endpoint is
  app-orphaned; live only).
- `htr_mod` preset/comfort writes — the 2.41.0 app sends the mixed-category
  body (`on`+`mode`+`selected_temp`+`comfort_temp`+`eco_offset`), so the
  shape is app-evidenced; live acceptance still needs an htr_mod unit.
- PMO cadence, `pmo_system` frames and power-limit behavior — needs a pmo
  unit.
- Boost write path: the app's dispatch is implemented from the 2.41.0
  bundle — `boost_config` 1/2 use status/setup, 3 (FULL) posts
  `/boost_mode {active, mode, temp?, time?}`. Live acceptance on a
  full-boost unit remains open.
- Full-boost UI: Runback/Advanced-boost presets are gated by
  `boost_modes_available`; the number entities remain defaults, the
  climate target is the active temperature, and `boost_mode`/
  `boost_end_time`/`boost_remaining_minutes` are attributes.
  `smartbox.set_boost_mode` covers active duration and explicit modes.
- Live 2026-10-20 (htr fw 1.9): `max_stemp_limit "0.0"` write accepted
  and persisted (restored `"24.0"`), and the app-shaped 4-key
  `/status` prog-temp body accepted and echoed (originals restored).
  The sentinel's enforcement is confirmed: with the ceiling at `"24.0"`,
  `stemp 24.5` clamped to `"24.0"`; with `"0.0"`, the same setpoint was
  accepted and echoed. The dedicated `/prog_temps` endpoint remains open.
- Programme-temperature writes now apply the app's per-profile clamp,
  ice/eco/comfort offset, and post-limit correction; the max-limit
  switch still deliberately restores the last remembered limit rather
  than the app's global maximum. Live 2026-10-20: lowering the ceiling
  from 24 to 19 pushed the corrected `18.0/18.5/19.0` status frame;
  both values were restored.
- Statistics completion is recorded when the rows are queued to the
  recorder, not when the recorder commits them. A recorder job dropped
  after queueing would leave that node marked imported; revisit only if
  recorder failures are observed.

Record results in `docs/api-contract.md` §7 as they are worked.

## Pending decisions

- Release target: the working assumption is the upstream
  `ajtudela/hass-smartbox`, so `manifest.json`, `pyproject.toml`,
  `README.md`, `CHANGELOG.md` and `const.GITHUB_ISSUES_URL` all point there.
  If the rewrite is not accepted upstream, the fork becomes the release
  target and those URLs, the README badges and `codeowners` must move
  together.
- Minimum Home Assistant: 2026.5.0 is the supported floor; `tox -e min`
  (CI job `minimum-home-assistant`) pins the harness that exercises it.

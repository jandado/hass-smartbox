# Project status

> **Role.** The short, curated record of what is in flight: the state of the
> released 3.0.0 line, the 3.1 split, post-release watch, and the open items
> that wait on hardware or on a decision. Long-lived facts live in the code
> and the other `docs/` files; completed work lives in git.

*Updated 2026-11-04. Keep this a 90-second read and prune entries as they
close.*

## State of the 3.1 split

- The integration branch `split-library` carries `3.1.0-rc.1`: imports use
  `smartbox.protocol` / `smartbox.core`, the vendored copies are removed,
  and `manifest.json` pins
  `smartbox@.../archive/refs/tags/v3.0.0-rc.1.tar.gz`.
- The library branch `3.0` carries `3.0.0rc1` with the extracted protocol
  and core, the ported tests and the optional `cli` extra; the
  `v3.0.0-rc.1` tag is pushed.
- `3.1.0-rc.1` pins the library with the no-space requirement format
  (`smartbox@https://...`), which local hassfest accepts. CI reruns on the
  amended tag.
- No entity, service or wire behaviour changes are intended in the split.
  The integration suite and the library suite are green against the editable
  library.
- Release order: tag/push the library RC first, then the integration RC, then
  test HACS/HAOS with the real tarball. CI installs the library tag in the
  tox environments.
- The public forum announcement remains on hold; the DM to @ajtudela has
  been sent.

## State of the 3.0.0 line

- `3.0.0` was released from the fork's `rewrite` branch (2026-11-03); the
  stable tag is at `eac76f7`. `main` is the default branch and carries
  `ed3830f`, three dev-only commits after the tag: two Dependabot lockfile
  bumps and the Codecov guard for Dependabot PRs. Development continues on
  `main`; `rewrite` is a historical ref at the same commit and can be
  archived or deleted. The upstream 2.6.x line remains at
  `ajtudela/hass-smartbox`.
- The `3.0.0-rc.1` through `3.0.0-rc.4` tags remain in Git history. The
  stable tag carries the rc.4 functional code; only release metadata and
  documentation changed.
- The rc series ran on the maintainer's installation through the rewrite;
  the final rc.4 code completed without Smartbox log errors or warnings.
  The stale-device code is unchanged from rc.3.
- The fork is the release target for now. Upstream authorship and codeowner
  credit are preserved (`@ajtudela`, `@Delmael`, `@jandado`); upstream
  collaboration is welcome.
- The fork's default branch is `main`; the scheduled hassfest/HACS checks
  run weekly on it with manual dispatch. The 2026-11-03 public-app check is
  recorded below.
- CI on `main` is green through `ed3830f`: ruff lint and format, strict
  mypy, hassfest, HACS, pytest with the 95% branch-coverage gate on the
  newest allowed Home Assistant, `tox -e min` on the declared 2026.5.0 floor,
  and both Codecov uploads. Dependabot PRs run the tests without the coverage
  upload, which needs a repository secret they do not receive. The split
  branch's CI is pending the library tag.
- The quality-scale audit is complete except the `brands` submission (logo
  assets and the `home-assistant/brands` entry); the manifest keeps
  `"quality_scale": "custom"` until it lands. The submission is postponed.
- The Tevolve tenant (`api-tevolve`, serial 15) is selectable. Its
  constants came from the vendor web bundle; a live login is still
  unverified.
- Public web-app transport check (2026-10-20, re-checked 2026-11-03):
  11 of the 13 provider apps ship the `ws_user` client; the EHC and
  Electrorad apps still serve the legacy 19 MB socket.io bundle, and
  unauthenticated `/api/v2/ws_user` probes return 404 on both API hosts.
  The per-account setup probe remains authoritative.

## Post-release watch

- Watch issue reports from fork users and the upstream thread.
- Stale-device cleanup (unchanged since rc.3): the unpair, power-cut and
  re-pair checks are the only release-adjacent live checks not recorded
  here. Record the results when they are exercised.
- Token reconnect: rc.4 showed the expected quiet cut every 4 hours.
  Revisit only if a cut lasts far longer than ~4 s or happens more often
  than every 4 h.
- Write confirmation: the 6 s countdown starts when the POST returns; the
  in-flight hold covers a token refresh or a slow request. Revisit if writes
  to a box still look slow.

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
  accepted and echoed. The dedicated `/prog_temps` endpoint is dormant:
  the vendor apps still use the 4-key `/status` body, so revisit it only
  if a future Android app release calls the endpoint.
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

- Release target: the fork `jandado/hass-smartbox` is the target for the
  3.0.0 line. `manifest.json`, `pyproject.toml`, the README badges and
  `const.GITHUB_ISSUES_URL` point there; upstream authorship and codeowner
  credit are preserved. Revisit if upstream adopts the rewrite or the fork
  becomes a separate project.
- Minimum Home Assistant: 2026.5.0 is the supported floor; `tox -e min`
  (CI job `minimum-home-assistant`) pins the harness that exercises it.

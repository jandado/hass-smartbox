# Project status

> **Role.** The short, curated record of what is in flight: current work,
> post-release watch, and the open items that wait on hardware or on a
> decision. Long-lived facts live in the code and the other `docs/` files;
> completed work lives in git. Release history and CI status live in
> `CHANGELOG.md`, tags/releases and GitHub Actions; they do not belong here.

*Updated 2026-11-10. Keep this a 90-second read and prune entries as they
close.*

## Bundled program card

- `custom:smartbox-program-card` ships from
  `custom_components/smartbox/frontend/`. Component-level `async_setup`
  serves it and loads it as an extra frontend module when the asset exists;
  no separate HACS item or Lovelace resource is involved.
- The card calls the existing `smartbox.set_schedule` service with changed
  days only; the integration needs no new backend API.
- The card is plan-only: a seven-column week grid (days across, time slots
  down), starting on the user's Home Assistant first day of the week. Day
  keys are Sunday..Saturday (HA's weekday index). It deliberately leaves the
  heater's mode, current target and overrides to the climate entity, and
  does not render a "now" or "next" state derived from the programme. The
  card targets the heater through its Schedule sensor; the visual editor
  lists the available sensors. A Legend switch (on by default) hides the
  profile legend.
- The preview merges equal neighbouring slots into continuous bands and
  labels 00/06/12/18 in a 30px left gutter. The plot's four corner cells are
  rounded, the chart container itself is square, and the gutter runs flush
  against the first displayed day with no divider. The day header has no
  vertical dividers, and rules cross the plot at 06, 12 and 18. The card
  header shows the heater name or configured title. The editor keeps the
  per-slot grid and follows the same treatment: no header dividers, a flush
  time gutter carrying the labels alone, 2px guides at 06/12/18 (3px on
  phones) and rounded plot corner cells in a square chart frame. The
  full-week chart renders in the dialog; the dialog body scrolls when it
  does not fit, so at most one scrollbar is present, and on phone widths the
  gutter and day columns narrow so the week fits without sideways scrolling.
  Phone widths also thicken the grid and day separators and raise their
  contrast so they remain visible on high-density screens. Its copy row
  reads "Copy from <day> to", one instruction line covers painting and
  copying, and the footer holds Apply alone; the X button and Escape close
  the dialog. Day headers name their copy-source action, and the reset
  button stays disabled while the draft matches the applied week. Legend
  and brush swatches share the rounded-square shape.
- Profile fills mix their hue with the card surface at 85% (`color-mix`) so
  they follow the theme, and `--smartbox-frost-color`,
  `--smartbox-eco-color` and `--smartbox-comfort-color` replace a fill. The
  editor's sticky day header and time gutter use the dialog surface instead
  of a gray frame.
- The programme editor uses the frontend's native `ha-dialog`. The header,
  close button, focus handling, animations and footer are HA's, not a custom
  backdrop.
- Closing a changed draft (X button or Escape) opens a second
  native `ha-dialog` shown as an alert: "Discard unsaved changes?" with the
  dismiss button focused and Discard marked danger. The card keeps that
  decision in a dialog because HA's editors guard unsaved changes the same
  way; inline banners stay for status such as a failed save.
- The card shell uses `ha-card`, the edit affordance uses `ha-icon`, and the
  dialog footer uses `ha-button`. The week chart and legend are custom; HA
  has no schedule-grid component. The card configuration editor uses
  `ha-form`.
- Browser-verified against a mocked Home Assistant frontend: desktop and
  mobile layouts, the flipped 7-column grid with sticky day headers and time
  rows, the grid as a keyboard-focusable edit control with a hover overlay,
  24 and 48 slots, apply diff, pointer drag, touch tap, keyboard navigation,
  undo, copy day, external-change handling, save failure, German strings,
  unsupported format, no schedule and unavailable states. The discard
  confirmation is covered across the X button and Escape, with both
  outcomes, focus return and no service call on discard. The visual pass is
  covered too: band rows without gaps, the 24/48-slot time axis,
  surface-aware fills at 85% and their CSS variable overrides, the unified
  legend/brush swatches, the legend switch, the copy-source label and
  hint, the subtitle-free card header, the removed line above 00, the four
  rounded plot corner cells with a flush gutter–first-day edge, the
  divider-free day header, the 06/12/18 hour rules, the same editor chart
  treatment at 24/48 slots (header, flush gutter, 2px guides, 3px on phones,
  rounded corners, square frame, full-week chart with the dialog body as the
  only scroller), editor axes using the dialog surface, the merged
  instruction line, the Apply-only footer, the phone-width grid without
  horizontal overflow, copy-source labels and selection state on the day
  headers, the reset state, live banner regions, the phone-width line
  thickness and contrast, and the softened editor hover.
- Open: the unverified `acm`/`htr_mod` profile mapping.

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
- Public forum announcement remains on hold.

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
- Tevolve tenant (`api-tevolve`, serial 15) is selectable. Its constants
  came from the vendor web bundle; a live login is still unverified.
- Quality-scale brands submission (logo assets and the
  `home-assistant/brands` entry) is postponed; the manifest keeps
  `"quality_scale": "custom"` until it lands.

Record results in `docs/api-contract.md` §7 as they are worked.

## Pending decisions

- Release target: the fork `jandado/hass-smartbox` is the target for the
  3.0.0 line. `manifest.json`, `pyproject.toml`, the README badges and
  `const.GITHUB_ISSUES_URL` point there; upstream authorship and codeowner
  credit are preserved. Revisit if upstream adopts the rewrite or the fork
  becomes a separate project.
- Minimum Home Assistant: 2026.5.0 is the supported floor; `tox -e min`
  (CI job `minimum-home-assistant`) pins the harness that exercises it.
- Marketplace/HACS metadata: the 2026.11 release replaces HACS with
  Marketplace. The repository metadata and HACS Action are unchanged at the
  2026.5 baseline; revisit when 2026.11 becomes the minimum.

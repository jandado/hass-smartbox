# Project status

> **Role.** The short, curated record of what is in flight: the state of the
> 3.0.0 line, what the soak is watching, and the open items that wait on
> hardware or on a decision. Long-lived facts live in the code and the other
> `docs/` files; completed work lives in git.

*Updated 2026-10-19. Keep this a 90-second read and prune entries as they
close.*

## State of the 3.0.0 line

- The rewrite is complete and pushed on branch `rewrite` (over the released
  2.6.0); `main` carries the released 2.6.x line until the maintainer
  releases 3.0.0.
- The 3.0.0 soak (extended live monitoring) runs on the maintainer's Home
  Assistant box, deployed from a `git archive` of the branch; the release
  decision waits on the soak.
- CI on the branch is green: ruff, mypy, hassfest, HACS, pytest on the
  newest allowed Home Assistant, and both Codecov uploads.

## What the soak is watching

- The 2.6.0 checklist still applies: one socket for all devices, the
  availability windows, a write mid-soak, the 4 h token reconnect.
- Write confirmation: the window starts at the POST, so a slow POST eats
  into the 6 s timeout — revisit if writes to a box look slow.
- Token reconnect: revisit only if a cut lasts far longer than ~4 s or
  happens more often than every 4 h.

## Open items (hardware-gated)

`docs/api-contract.md` §7 is canonical for the wire questions and their
statuses. The ones that need the maintainer's hardware — or a season:

- `window_open` retry, cold night only: open the window AND door BEFORE
  starting heat, setpoint 23 °C, outside below 5 °C; success is
  `window_open: true` in the frame plus the heater self-pausing (active
  false, duty 0) while the temperature keeps falling.
- RTC `n` month indexing — waits for December (the endpoint is
  app-orphaned; live only).
- `htr_mod` preset/comfort writes — needs an htr_mod unit.
- PMO cadence, `pmo_system` frames and power-limit behavior — needs a pmo
  unit.
- Boost write path (`/boost` vs `/boost_mode` vs the `/status` body) —
  needs a boost-capable unit; `extra_options` replace-vs-merge rides along.
- Not yet probed on the 3.0.0 stack: the max-limit `"0.0"` toggle
  round-trip and the 4-key prog-temp status body.

Record results in `docs/api-contract.md` §7 as they are worked.

## Pending decisions

- Release URLs: `README.md`, `CHANGELOG.md` and `const.GITHUB_ISSUES_URL`
  point at the upstream repository while development continues on the fork;
  revisit at release time.

# Architecture

How the integration is put together, and why it is shaped this way. It is
written for a reader who knows Home Assistant but not Smartbox, and for one
who knows the vendor's cloud but not this codebase. Companion documents:
[README.md](README.md) for user-facing behavior, [CONTRIBUTING.md](CONTRIBUTING.md)
for the workflow and the wire-fact discipline, and
[docs/api-contract.md](docs/api-contract.md) for the wire record itself.

## What it is

A Home Assistant custom integration (domain `smartbox`) for Smartbox electric
heaters. It talks to the vendor's cloud directly: REST calls for reads and
writes, plus one `ws_user` websocket per account for pushed updates. A "box"
(the site gateway) carries several heater nodes; both become Home Assistant
devices.

The integration is self-contained. The wire protocol is vendored under
`custom_components/smartbox/`, and `manifest.json` ships no requirements:
nothing is installed alongside it at runtime.

## The three layers

The split keeps two kinds of churn apart: the vendor's cloud (endpoints,
frame shapes, timing quirks) and Home Assistant (entity model, lifecycle,
APIs). Wire knowledge lives in one package, Home Assistant knowledge lives in
one package, and the state between them is plain Python, testable without
either. The import directions are enforced by `tests/test_layering.py` (an
import scan plus a fresh-interpreter probe), so the rule is checked, not
aspirational.

### `smartbox_protocol/` — everything that knows the wire

The only layer that touches the network:

- `auth.py`, `jwt.py` — the token lifecycle: one grant per password, refresh
  rotation, serialized token calls.
- `client.py` — the REST client: reads, plus the named write operations from
  `writes.py`, addressed via `paths.py`.
- `transport.py`, `ws_user.py` — the transport seam and the `ws_user` socket:
  a fresh token per connect, escalating backoff, a connect watchdog, bounded
  teardown.
- `values.py` — wire normalization: units, quantization, the schedule shape;
  one owner per rule.
- `reseller.py`, `retry.py`, `timing.py`, `errors.py`, `events.py` — the brand
  table, the backoff helper, the timing seam every wait and clock read goes
  through, the error hierarchy, and the typed events the transport emits.

Wire rules are recorded with provenance in `docs/api-contract.md`. Code
comments cite the record instead of restating a rule as folklore, and new
findings are verified with `tools/live_probe.py` before they are believed.

### `smartbox_core/` — the domain, without Home Assistant

The state of the home, independent of the HA entity model:

- `state.py` — the versioned, copy-on-write store: the single source of
  truth.
- `merging.py` — the pure merge rules for snapshots and pushed frames.
- `engine.py` — ingestion (with exact-equality dedup), the write path
  (optimistic merge, per-node serialization, per-slot confirmation), and
  supervision.
- `deadlines.py` — write-confirmation and lost-window bookkeeping, served by
  one timer.
- `feed.py` — the feed monitor: close grace and outage episodes.

It imports the protocol for event types, errors, value comparison and the
timing seam — nothing else from the integration — and never imports Home
Assistant.

### the HA adapter — the only layer that imports Home Assistant

`custom_components/smartbox/*.py`: runtime assembly and the entry lifecycle
(`__init__.py`), the shared entity bases and accessors over engine state
(`adapter.py`), the six entity platforms, services, setup and reauth
(`config_flow.py`), unit adaptation for the entities, the energy statistics
import on first setup (`history.py`), diagnostics and system health.

It renders store state into entities and translates entity and service
actions into engine operations. It holds no wire knowledge of its own.

## Repository map

```
custom_components/smartbox/   the integration
├── smartbox_protocol/        the wire layer
├── smartbox_core/            the domain layer
├── __init__.py               entry lifecycle, runtime assembly, services
├── adapter.py                entity bases and accessors over engine state
├── identity.py               discovery results; capability gates
├── mapping.py                schedule/RTC/boost decoding; service helpers
├── config_flow.py            setup and reauth
├── climate.py, sensor.py, number.py, switch.py,
│   select.py, binary_sensor.py   the six entity platforms
├── history.py                the energy statistics import
├── diagnostics.py, system_health.py
├── const.py                  domain constants, node types, defaults
├── reseller.py               entry data → reseller identity
├── manifest.json, icons.json, brand/, services.yaml,
│   strings.json, translations/

tests/                        the flat suite; its scaffolding lives beside it
├── conftest.py               fixtures: the mock world via the client seams,
│                             runs parametrized C/F
├── const.py, mocks.py        the mock world's data; entity-id formulas
├── test_utils.py             expected-value helpers (rounding, conversion)
├── stack.py                  seed_stack plus frame/push/confirm helpers
├── fakes.py, fake_api.py, fake_transport.py
│                             the fake server and transport
├── fixtures/live/            captures from the real cloud
└── test_*.py                 the tiers tabled below

tools/live_probe.py           CLI for probing the real cloud
docs/                         the wire records (see "Keeping the record honest");
                              status.md is the curated live state
.github/workflows/            CI: tests, lint, type checks, HACS, hassfest
pyproject.toml, uv.lock       metadata and the locked development environment
hacs.json, codecov.yml        packaging and coverage configuration
```

## How it works

### The read path

1. Entry setup probes authentication and `ws_user` support, reads device and
   node identity over REST, and builds the store.
2. The socket delivers an initial snapshot and, after that, update frames;
   REST reads serve what the snapshot does not carry, and three timers poll
   what the wire never pushes: total consumption, PMO power and the RTC
   clock drift.
3. Every frame is merged into the store by the pure merge rules. A frame for
   a device or node the snapshot does not hold is dropped: nothing about the
   account is invented from a frame.
4. Store changes fan out to the entities, which render straight from the
   store — there is no entity-level cache.

Availability moves on evidence: a lost marker opens an unavailable window, an
ok frame clears it, and a socket close opens a grace window that a fresh
frame clears silently — only an expiring one becomes a reported outage.

### The write path

1. Entities and services call named engine operations; these are the only
   write entry points.
2. An operation posts over REST. The engine merges the posted body over the
   store optimistically and serializes writes per node, because a setup or
   programme POST carries the whole object and two in flight would overwrite
   each other's keys.
3. Confirmation is armed as the POST goes out. The confirming frame retires
   the window; a window that expires treats the node as unavailable until it
   reports again. A write that changes nothing arms nothing, because the wire
   pushes no frame for a value-equal write.

## Invariants worth knowing before touching anything

- The wire record is `docs/api-contract.md`. New wire facts are probed,
  recorded, and only then encoded; comments cite the section. "It works" is
  not the acceptance bar — the record is.
- The store is the single source of truth; entities hold no state of their
  own.
- Frames never create state, and an empty body is not evidence: a frame for
  an unknown device or node is dropped, and an empty payload neither replaces
  a stored slot nor counts as reachability.
- Availability only moves on evidence — lost markers, ok frames, the close
  grace — so a stale value cannot keep rendering as available.
- Writes are serialized per node, applied optimistically, and confirmed per
  slot; each confirmation is generation-owned, so overlapping writes do not
  disarm each other.
- Time is a seam: every wait and clock read goes through
  `smartbox_protocol/timing.py`, which is how tests exercise timeout logic
  without waiting.
- Teardown is total: unloading an entry clears its deadlines, timers and
  listeners; nothing outlives the config entry.
- Layers import downward only — adapter → core → protocol — and the protocol
  layer stays free of Home Assistant and of the other layers.
- Module docstrings are the contract for their module — the engine's
  ordering rules, the named writes' wire rules, the feed's liveness rules;
  read them before changing behaviour there.
- Entity identities are stable: `translation_key` mirrors the per-entity
  key and `unique_id` is `<node_id>_<key>` (or `<dev_id>_<key>` for box
  entities); changing a formula costs users their history.
- Numbers carry the device's own C/F scale bounds (`_DeviceScaleBounds`);
  the away offset is a delta and deliberately has no temperature device
  class (HA would convert a classed number as an absolute temperature).
- Shipped text may reference the repo's own history (versions, earlier
  commits) where that helps the reader; it cites the durable docs and
  never points at agent instructions or files that do not ship in the
  repo.
- User-facing strings are ICU messages in the Home Assistant frontend:
  braces are placeholders there, so a stray brace renders as a translation
  error. A gate keeps the translation files and `services.yaml` free of
  stray braces.

## Tests, and what each tier is for

| Tier | Files | What it holds |
| --- | --- | --- |
| Wire units | `test_client`, `test_auth`, `test_writes`, `test_values`, `test_mapping`, `test_ws_user_transport` | The wire rules: request shapes, body forms, normalization, the token lifecycle. Expected values trace to the contract record. |
| Domain units | `test_core_engine`, `test_core_availability`, `test_core_writes`, `test_core_state` | Store merging and engine behavior, exercised with no Home Assistant involved. |
| Platform behavior | `test_climate`, `test_sensor`, `test_number`, `test_switch`, `test_select`, `test_binary_sensor`, `test_init`, `test_availability`, `test_config_flow`, `test_diagnostics`, `test_system_health`, `test_identity`, `test_adapter`, `test_ws_user_wiring` | Real service calls and entity states. Only the wire endpoints are fakes; the engine, store and adapter under them are the real code. |
| Wire reality | `test_loopback` | The real client against a real local aiohttp server: pins what the transport fakes assume about handshakes, closes and timeout shapes. |
| Chaos | `test_soak` | The whole stack against a hostile local server — kills, stalls, 5xx, lost frames — asserting structural invariants: no stuck deadline, no leaked timer, silent teardown. |
| Gates and tooling | `test_layering`, `test_docs_references`, `test_translations`, `test_version`, `test_lib_contract`, `test_payload_bounding`, `test_live_probe`, `test_fake_api`, `test_fake_transport` | Architecture, documentation and tooling rules, and the fakes' own behavior. |

One caveat matters more than the rest: the mock world is a regression
baseline, not a wire oracle. It encodes the account as the contract records
it, and it cannot tell us that the record is wrong. When behavior and the
record disagree, the record wins until a probe says otherwise — that is what
`tools/live_probe.py` and the captures under `tests/fixtures/live/` are for.

## Working in the repo

Setup and the three gates — [CONTRIBUTING.md](CONTRIBUTING.md) has the rest:

```
uv sync --all-extras --dev
.venv/bin/python -m pytest -q      # all cores; -n0 for a single test
.venv/bin/ruff check custom_components tests tools
.venv/bin/mypy custom_components tools
```

**Probing the real cloud.** `tools/live_probe.py` drives the real hosts for
wire investigation (`auth`, `probe`, `reads`, `ws`, `write`, `all`). It reads
the user's own gitignored `.env` at the repo root — nothing here prints or
commits that file — and the write commands refuse to run without an explicit
acknowledgement flag, because they move real heaters.

**Where a change goes.**

- New entity or attribute: put the state in the core (merge rules), expose it
  through an adapter accessor, render it in the platform entity, and test it
  in the matching tier.
- New write: a named operation in `smartbox_protocol/writes.py`, its slot and
  path in the engine, the entity or service surface, and a test pinning the
  POST body.
- New wire field: probe it, record it in `docs/api-contract.md`, normalize it
  in `values.py` or `merging.py`, then surface it upward.

The lower half of the stack never needs Home Assistant to be testable.

## Keeping the record honest

Three documents, three roles: `docs/api-contract.md` settles wire acceptance
— probed facts with provenance tags, open questions, recorded divergences —
`docs/webapi-spec.md` holds the decoded web app's statics, and
`docs/api-ws-user.md` holds the current vendor apps' behavior plus a parity
checklist to re-run per vendor app release. Where an app-derived document and
a live observation disagree, the live observation wins, and the disagreement
is recorded rather than silently resolved. [CONTRIBUTING.md](CONTRIBUTING.md)
describes the discipline in full; the gates here keep it enforced — every
citation resolves against the in-repo copy, and shipped text stays free of
process narrative.

## Coming from the `smartbox` library

The pieces this integration needs from the `smartbox` library live in the
vendored layers: the reseller table (`smartbox_protocol/reseller.py`), the
node-type enum (`const.py`) and the backoff helper
(`smartbox_protocol/retry.py`). `tests/test_lib_contract.py` pins the values
shared with the library, so drift is visible. The library is not a runtime
dependency: `manifest.json` ships no requirements.

## Where to go deeper

| Question | Document |
| --- | --- |
| What does the vendor cloud do, per endpoint and frame? | `docs/api-contract.md` |
| What do the current vendor apps do? | `docs/api-ws-user.md` |
| What did the vendor's web app code do? | `docs/webapi-spec.md` |
| How do I set up and contribute? | `CONTRIBUTING.md` |
| How is the integration used? | `README.md` |
| How should an agent work in this repo? | `AGENTS.md` |
| What is in flight right now? | `docs/status.md` |
| How do I verify a claim against the real cloud? | `tools/live_probe.py` |
| What changed in each release? | `CHANGELOG.md` |
